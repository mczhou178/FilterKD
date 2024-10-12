# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from gzip import FTEXT
import math
import random
import os
from dataclasses import dataclass, field

import torch
from fairseq import metrics, utils
from fairseq.criterions import FairseqCriterion, register_criterion
from fairseq.dataclass import FairseqDataclass
from omegaconf import II


@dataclass
class LabelSmoothedCrossEntropyCriterionConfig(FairseqDataclass):
    label_smoothing: float = field(
        default=0.0,
        metadata={"help": "epsilon for label smoothing, 0 means no label smoothing"},
    )
    result_path: str = field(
        default='',
    )
    topk: int = field(
        default=-1,
        metadata={"help": "epsilon for label smoothing, 0 means no label smoothing"},
    )
    report_accuracy: bool = field(
        default=False,
        metadata={"help": "report accuracy metric"},
    )
    ignore_prefix_size: int = field(
        default=0,
        metadata={"help": "Ignore first N tokens"},
    )
    sentence_avg: bool = II("optimization.sentence_avg")


def label_smoothed_nll_loss(lprobs, target, epsilon, ignore_index=None, reduce=True):
    if target.dim() == lprobs.dim() - 1:
        target = target.unsqueeze(-1)
    nll_loss = -lprobs.gather(dim=-1, index=target)
    smooth_loss = -lprobs.sum(dim=-1, keepdim=True)
    if ignore_index is not None:
        pad_mask = target.eq(ignore_index)
        nll_loss.masked_fill_(pad_mask, 0.0)
        smooth_loss.masked_fill_(pad_mask, 0.0)
    else:
        nll_loss = nll_loss.squeeze(-1)
        smooth_loss = smooth_loss.squeeze(-1)
    if reduce:
        nll_loss = nll_loss.sum()
        smooth_loss = smooth_loss.sum()
    eps_i = epsilon / (lprobs.size(-1) - 1)
    loss = (1.0 - epsilon - eps_i) * nll_loss + eps_i * smooth_loss
    loss = loss + ((1 - epsilon) * math.log(1 - epsilon) + eps_i * math.log(eps_i) * (lprobs.size(-1) - 1))
    return loss, nll_loss


@register_criterion(
    "record_topk_predictions", dataclass=LabelSmoothedCrossEntropyCriterionConfig
)
class LabelSmoothedCrossEntropyCriterion(FairseqCriterion):
    def __init__(
        self,
        task,
        sentence_avg,
        label_smoothing,
        result_path,
        topk,
        ignore_prefix_size=0,
        report_accuracy=False,
    ):
        super().__init__(task)
        self.sentence_avg = sentence_avg
        self.eps = label_smoothing
        self.result_path = result_path
        self.topk = topk
        self.ignore_prefix_size = ignore_prefix_size
        self.report_accuracy = report_accuracy

    def forward(self, model, sample, teacher_model=None, reduce=True):
        """Compute the loss for the given sample.

        Returns a tuple with three elements:
        1) the loss
        2) the sample size, which is used as the denominator for the gradient
        3) logging outputs to display while training
        """
        model.eval()
        student_enc_out = model.encoder(
            sample["net_input"]["src_tokens"], 
            src_lengths=sample["net_input"]["src_lengths"], 
            return_all_hiddens=True
        )
        student_dec_out = model.decoder(
            sample["net_input"]["prev_output_tokens"],
            encoder_out=student_enc_out,
            features_only=False,
            src_lengths=sample["net_input"]["src_lengths"],
            return_all_hiddens=False,
        )

        mle_loss, nll_loss = self.compute_loss(model, student_dec_out, sample, reduce=reduce)
        
        if teacher_model is not None:
            with torch.no_grad():
                teacher_model.eval()
                # teacher_output = teacher_model(**sample["net_input"])
                teacher_enc_out = teacher_model.encoder(
                    sample["net_input"]["src_tokens"], 
                    src_lengths=sample["net_input"]["src_lengths"], 
                    return_all_hiddens=True
                )
                teacher_dec_out = teacher_model.decoder(
                    sample["net_input"]["prev_output_tokens"],
                    encoder_out=teacher_enc_out,
                    features_only=False,
                    src_lengths=sample["net_input"]["src_lengths"],
                    return_all_hiddens=False,
                )
            teacher_logits = teacher_dec_out[0]
            student_logits = student_dec_out[0]
            _, teacher_topk_preds = torch.topk(teacher_logits, k=self.topk, dim=-1)
            _, student_topk_preds = torch.topk(student_logits, k=self.topk, dim=-1)
            result_path = self.result_path
            if not os.path.exists(result_path):
                os.mkdir(result_path)
            teacher_preds = teacher_logits.argmax(-1)
            student_preds = student_logits.argmax(-1)
            acc = teacher_logits.argmax(-1).eq(sample['target'])
            with open(os.path.join(result_path, f'teacher_top{self.topk}_preds.txt'), 'a+') as f_tea, \
                open(os.path.join(result_path, f'student_top{self.topk}_preds.txt'), 'a+') as f_stu, \
                open(os.path.join(result_path, 'golden_targets.txt'), 'a+') as f_gold:
                for i in range(sample['target'].shape[0]):
                    for j in range(sample['target'].shape[1]):
                        if sample['target'][i, j] != self.padding_idx:
                            tea_line = ' '.join(list(map(str, teacher_topk_preds[i, j].tolist()))) + '\n'
                            stu_line = ' '.join(list(map(str, student_topk_preds[i, j].tolist()))) + '\n'
                            gold_line = str(sample['target'][i, j].item()) + '\n'
                            f_tea.write(tea_line)
                            f_stu.write(stu_line)
                            f_gold.write(gold_line)
            
            loss = mle_loss * 0.0
            kd_loss1 = torch.zeros_like(loss)
            kd_loss2 = torch.zeros_like(loss)
            
        else:
            loss = mle_loss
            kd_loss1 = torch.zeros_like(loss)
            kd_loss2 = torch.zeros_like(loss)

        sample_size = (
            sample["target"].size(0) if self.sentence_avg else sample["ntokens"]
        )

        logging_output = {
            "loss": loss.data,
            "mle_loss": mle_loss.data,
            "nll_loss": nll_loss.data,
            "kd_loss1": kd_loss1.data,
            "kd_loss2": kd_loss2.data,
            "ntokens": sample["ntokens"],
            "nsentences": sample["target"].size(0),
            "sample_size": sample_size,
        }
        if self.report_accuracy:
            n_correct, total = self.compute_accuracy(model, net_output, sample)
            logging_output["n_correct"] = utils.item(n_correct.data)
            logging_output["total"] = utils.item(total.data)
        return loss, sample_size, logging_output

    def get_lprobs_and_target(self, model, net_output, sample):
        lprobs = model.get_normalized_probs(net_output, log_probs=True)
        target = model.get_targets(sample, net_output)
        if self.ignore_prefix_size > 0:
            if getattr(lprobs, "batch_first", False):
                lprobs = lprobs[:, self.ignore_prefix_size :, :].contiguous()
                target = target[:, self.ignore_prefix_size :].contiguous()
            else:
                lprobs = lprobs[self.ignore_prefix_size :, :, :].contiguous()
                target = target[self.ignore_prefix_size :, :].contiguous()
        return lprobs.view(-1, lprobs.size(-1)), target.view(-1)

    def compute_loss(self, model, net_output, sample, reduce=True):
        lprobs, target = self.get_lprobs_and_target(model, net_output, sample)
        loss, nll_loss = label_smoothed_nll_loss(
            lprobs,
            target,
            self.eps,
            ignore_index=self.padding_idx,
            reduce=reduce,
        )
        return loss, nll_loss

    def compute_accuracy(self, model, net_output, sample):
        lprobs, target = self.get_lprobs_and_target(model, net_output, sample)
        mask = target.ne(self.padding_idx)
        n_correct = torch.sum(
            lprobs.argmax(1).masked_select(mask).eq(target.masked_select(mask))
        )
        total = torch.sum(mask)
        return n_correct, total

    @classmethod
    def reduce_metrics(cls, logging_outputs) -> None:
        """Aggregate logging outputs from data parallel training."""
        loss_sum = sum(log.get("loss", 0) for log in logging_outputs)
        mle_loss_sum = sum(log.get("mle_loss", 0) for log in logging_outputs)
        nll_loss_sum = sum(log.get("nll_loss", 0) for log in logging_outputs)
        kd_loss1_sum = sum(log.get("kd_loss1", 0) for log in logging_outputs)
        kd_loss2_sum = sum(log.get("kd_loss2", 0) for log in logging_outputs)
        ntokens = sum(log.get("ntokens", 0) for log in logging_outputs)
        sample_size = sum(log.get("sample_size", 0) for log in logging_outputs)

        metrics.log_scalar(
            "loss", loss_sum / sample_size / math.log(2), sample_size, round=3
        )
        metrics.log_scalar(
            "mle_loss", mle_loss_sum / ntokens / math.log(2), ntokens, round=3
        )
        metrics.log_scalar(
            "nll_loss", nll_loss_sum / ntokens / math.log(2), ntokens, round=3
        )
        metrics.log_scalar(
            "kd_loss1", kd_loss1_sum / ntokens, ntokens, round=3
        )
        metrics.log_scalar(
            "kd_loss2", kd_loss2_sum / ntokens, ntokens, round=3
        )
        metrics.log_derived(
            "ppl", lambda meters: utils.get_perplexity(meters["nll_loss"].avg)
        )

        total = utils.item(sum(log.get("total", 0) for log in logging_outputs))
        if total > 0:
            metrics.log_scalar("total", total)
            n_correct = utils.item(
                sum(log.get("n_correct", 0) for log in logging_outputs)
            )
            metrics.log_scalar("n_correct", n_correct)
            metrics.log_derived(
                "accuracy",
                lambda meters: round(
                    meters["n_correct"].sum * 100.0 / meters["total"].sum, 3
                )
                if meters["total"].sum > 0
                else float("nan"),
            )

    @staticmethod
    def logging_outputs_can_be_summed() -> bool:
        """
        Whether the logging outputs returned by `forward` can be summed
        across workers prior to calling `reduce_metrics`. Setting this
        to True will improves distributed training speed.
        """
        return True
