# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import math
import numpy as np
import torch.nn.functional as F
import torch
from fairseq import metrics, utils
from fairseq.criterions import FairseqCriterion, register_criterion
import queue
import copy

from knnbox.common_utils import global_vars, select_keys_with_pad_mask, archs
from knnbox.datastore import Datastore
from knnbox.retriever import Retriever
from knnbox.combiner import Combiner


def label_smoothed_nll_loss(lprobs, target, epsilon, ignore_index=None, reduce=True):
    if target.dim() == lprobs.dim() - 1:
        target = target.unsqueeze(-1)
    nll_loss = -lprobs.gather(dim=-1, index=target)
    smooth_loss = -lprobs.sum(dim=-1, keepdim=True)
    if ignore_index is not None:
        pad_mask = target.eq(ignore_index)
        nll_loss.masked_fill_(pad_mask, 0.)
        smooth_loss.masked_fill_(pad_mask, 0.)
    else:
        nll_loss = nll_loss.squeeze(-1)
        smooth_loss = smooth_loss.squeeze(-1)
    if reduce:
        nll_loss = nll_loss.sum()
        smooth_loss = smooth_loss.sum()
    eps_i = epsilon / lprobs.size(-1)
    loss = (1. - epsilon) * nll_loss + eps_i * smooth_loss
    return loss, nll_loss


@register_criterion('label_smoothed_cross_entropy-adapter')
class LabelSmoothedCrossEntropyCriterionAdapter(FairseqCriterion):

    def __init__(self, task, sentence_avg, label_smoothing):
        super().__init__(task)
        self.sentence_avg = sentence_avg
        self.eps = label_smoothing
        self.range_eps = 0.01
        self.queue = torch.cuda.FloatTensor([])
        self.teacher_loss_queue = torch.cuda.FloatTensor([])
        self.real_distil_rate = 0.0
        self.dict_count = None
        self.prior_tau = 1.0
        self.datastore = Datastore.load("/home/mc/adapter-kd/datastore/base/deen/", load_list=["vals"])
        self.datastore.load_faiss_index("keys")
        self.retriever = Retriever(datastore=self.datastore, k=8)
        self.combiner = Combiner(lambda_=0.999,
                                 temperature=100, probability_dim=10152)

        self.num = 0
        self.teacher_model = None

        self.distil_strategy = 'direct'
        self.distil_rate = 0.8
        self.teacher_predict_temperature_schedule = None
        self.teacher_predict_temperature = 1.0
        self.difficult_queue_size = 5000
        self.high_freq_words = None

    @staticmethod
    def add_args(parser):
        """Add criterion-specific arguments to the parser."""
        # fmt: off
        parser.add_argument('--label-smoothing', default=0., type=float, metavar='D',
                            help='epsilon for label smoothing, 0 means no label smoothing')
        # fmt: on

    def push_to_FIFO_queue(self, tensor):
        tensor = tensor.detach().view(-1)
        tensor_size = tensor.size(0)
        current_size = self.queue.size(0)
        self.queue = self.queue.view(-1)
        if tensor_size + current_size < self.difficult_queue_size:
            self.queue = torch.cat((self.queue, tensor))
        else:
            self.queue = torch.cat((self.queue[tensor_size:], tensor))

    def push_to_teacher_FIFO_queue(self, tensor):
        tensor = tensor.detach().view(-1)
        tensor_size = tensor.size(0)
        current_size = self.teacher_loss_queue.size(0)
        self.teacher_loss_queue = self.teacher_loss_queue.view(-1)
        if tensor_size + current_size < self.difficult_queue_size:
            self.teacher_loss_queue = torch.cat((self.teacher_loss_queue, tensor))
        else:
            self.teacher_loss_queue = torch.cat((self.teacher_loss_queue[tensor_size:], tensor))

    def forward(self, model, sample, reduce=True, teacher_model=None, update_num=None):

        if self.num == 0:
            from fairseq.checkpoint_utils import load_model_ensemble
            checkpoints_paths = ['/home/mc/adapter-kd/ckpt/base2/deen/checkpoint_best.pt']
            new_models, _ = load_model_ensemble(checkpoints_paths)
            self.teacher_model = new_models[0]
            self.teacher_model.to('cuda:0')
            self.teacher_model.eval()

        self.num += 1
        net_output = model(**sample['net_input'])
        teacher_output = None
        if self.teacher_model is not None and self.distil_strategy != 'normal':
            with torch.no_grad():
                # teacher_output, query = model.forwards(**sample['net_input'])
                teacher_output, query = self.teacher_model.forwards(**sample['net_input'])
                self.retriever.retrieve(query, return_list=["vals", "distances"])

        loss, nll_loss, extra_result = self.compute_loss(model, net_output, sample, reduce=reduce,
                                                         teacher_output=teacher_output,
                                                         distil_strategy=self.distil_strategy,
                                                         update_num=update_num)
        sample_size = sample['target'].size(0) if self.sentence_avg else sample['ntokens']
        logging_output = {
            'loss': loss.data,
            'nll_loss': nll_loss.data if nll_loss is not None else loss.data,
            'ntokens': sample['ntokens'],
            'nsentences': sample['target'].size(0),
            'sample_size': sample_size,
            'distil_rate': self.real_distil_rate,
            'gpu_nums': 1,
            'KD_loss': extra_result['KD_loss'].data if extra_result.get('KD_loss', None) is not None else 0,
            'nll_loss_distil': extra_result['nll_loss_distil'].data if extra_result.get('nll_loss_distil',
                                                                                        None) is not None else 0,
            # 'distil_token_num': extra_result['distil_token_num'].data if extra_result.get('distil_token_num', None) is not None else 0,
        }

        return loss, sample_size, logging_output

    def get_teacher_probs(self, teacher_output):

        # print(torch.cuda.device_count())
        knn_prob = self.combiner.get_knn_prob(**self.retriever.results, device='cuda:0')
        # knn_prob = knn_prob.to(self.teacher_model.device)
        combined_prob, _ = self.combiner.get_combined_prob(knn_prob, teacher_output[0] / self.prior_tau,
                                                           log_probs=False)
        # distil_lprobs = combined_prob
        distil_lprobs = combined_prob.view(-1, combined_prob.size(-1))
        return distil_lprobs

    def get_teacher_probs_direct(self, teacher_output):
        distil_lprobs = self.teacher_model.get_normalized_probs(teacher_output, log_probs=False)
        distil_lprobs = distil_lprobs.view(-1, distil_lprobs.size(-1))

        return distil_lprobs

    def get_knn_and_teacher(self, teacher_output):
        # print(torch.cuda.device_count())
        knn_prob = self.combiner.get_knn_prob(**self.retriever.results, device='cuda:0')
        # knn_prob = knn_prob.to(self.teacher_model.device)
        kNN_prob, nmt_prob = self.combiner.get_no_combined_prob(knn_prob, teacher_output[0], log_probs=False)
        # distil_lprobs = combined_prob
        kNN_prob = kNN_prob.view(-1, kNN_prob.size(-1))
        nmt_prob = nmt_prob.view(-1, nmt_prob.size(-1))
        return kNN_prob, nmt_prob

    def js_div(self, p, q):
        mean = (p + q) / 2.0
        mean_log = mean.log()
        p_mean = F.kl_div(mean_log, p, reduction='none')
        q_mean = F.kl_div(mean_log, q, reduction='none')
        kd_loss = (p_mean + q_mean) / 2.0
        return kd_loss

    def compute_loss(self, model, net_output, sample, reduce=True, teacher_output=None, distil_strategy="normal",
                     update_num=None):
        probs = model.get_normalized_probs(net_output, log_probs=False)
        t_probs = model.get_normalized_probs((net_output[0] / self.prior_tau,), log_probs=False)
        t_lprobs = torch.log(t_probs)
        lprobs = torch.log(probs)
        t_probs = t_probs.view(-1, lprobs.size(-1))
        t_lprobs = t_lprobs.view(-1, lprobs.size(-1))
        probs = probs.view(-1, lprobs.size(-1))
        lprobs = lprobs.view(-1, lprobs.size(-1))
        target = model.get_targets(sample, net_output)
        bsz, seq_len = target.shape
        target = target.view(-1, 1)
        pad_mask = target.eq(self.padding_idx).view(-1)
        loss = None
        nll_loss = None
        extra_result = {}
        if distil_strategy == 'normal' or teacher_output is None:
            # not use distillation
            loss, nll_loss = label_smoothed_nll_loss(
                lprobs, target, self.eps, ignore_index=self.padding_idx, reduce=reduce,
            )
        elif distil_strategy == 'direct':
            golden_loss, nll_loss = label_smoothed_nll_loss(
                lprobs, target, self.eps, ignore_index=self.padding_idx, reduce=reduce,
            )
            nmt_prob = self.get_teacher_probs_direct(teacher_output)
            KL_loss = F.kl_div(lprobs, nmt_prob, reduction='none')
            KL_loss = KL_loss.sum(dim=-1)
            KL_loss.masked_fill_(pad_mask, 0.)
            KL_loss = KL_loss.sum()
            extra_result['KD_loss'] = KL_loss
            loss = KL_loss

        elif distil_strategy == 'adapter_direct':
            golden_loss, nll_loss = label_smoothed_nll_loss(
                lprobs, target, self.eps, ignore_index=self.padding_idx, reduce=reduce,
            )

            # 只蒸馏knn分布中目标标记的概率是最大值的情况
            nmt_prob = self.get_teacher_probs_direct(teacher_output)
            kNN_prob = self.get_teacher_probs(teacher_output)

            knn_target_values = kNN_prob.gather(1, target)

            # 获取knn_prob每行的最大值
            knn_max_values, _ = kNN_prob.max(dim=1, keepdim=True)

            # 创建一个布尔掩码，表示哪些行的target值大于每行的最大值
            mask = (knn_target_values >= knn_max_values).float()


            knn_prob = 0.5 * kNN_prob * mask
            nmt_prob = nmt_prob * (1 - 0.5 * mask)

            distil_lprobs = knn_prob + nmt_prob

            KL_loss = F.kl_div(lprobs, distil_lprobs, reduction='none')
            KL_loss = KL_loss.sum(dim=-1)
            KL_loss.masked_fill_(pad_mask, 0.)
            KL_loss = KL_loss.sum()
            extra_result['KD_loss'] = KL_loss
            loss = golden_loss + 2.0 * KL_loss

        return loss, nll_loss, extra_result

    @staticmethod
    def reduce_metrics(logging_outputs) -> None:
        """Aggregate logging outputs from data parallel training."""
        loss_sum = sum(log.get('loss', 0) for log in logging_outputs)
        nll_loss_sum = sum(log.get('nll_loss', 0) for log in logging_outputs)
        # kd_loss_sum = sum(log.get('KD_loss', 0) for log in logging_outputs)
        ntokens = sum(log.get('ntokens', 0) for log in logging_outputs)
        sample_size = sum(log.get('sample_size', 0) for log in logging_outputs)
        nll_loss_distil = sum(log.get('nll_loss_distil', 0) for log in logging_outputs)
        distil_token_num = sum(log.get('distil_token_num', 0) for log in logging_outputs)
        GPU_nums = sum(log.get('gpu_nums', 0) for log in logging_outputs)
        # real_distil_rate = sum(log.get('distil_rate', 0) for log in logging_outputs) / GPU_nums
        metrics.log_scalar('loss', loss_sum / sample_size / math.log(2), sample_size, round=3)
        metrics.log_scalar('nll_loss', nll_loss_sum / ntokens / math.log(2), ntokens, round=3)
        # metrics.log_scalar('kd_loss_sum', kd_loss_sum / distil_token_num, round=4)
        metrics.log_derived('ppl', lambda meters: utils.get_perplexity(meters['nll_loss'].avg))
        # metrics.log_scalar('distil_rate', real_distil_rate, round=4)

    @staticmethod
    def logging_outputs_can_be_summed() -> bool:
        """
        Whether the logging outputs returned by `forward` can be summed
        across workers prior to calling `reduce_metrics`. Setting this
        to True will improves distributed training speed.
        """
        return True