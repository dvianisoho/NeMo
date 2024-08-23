# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Copyright 2017 Johns Hopkins University (Shinji Watanabe)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import List, Optional, Tuple, Union

import numpy as np
import torch
from tqdm import tqdm

from nemo.collections.asr.modules import rnnt_abstract
from nemo.collections.asr.parts.submodules.rnnt_beam_decoding import pack_hypotheses
from nemo.collections.asr.parts.utils.rnnt_utils import Hypothesis, NBestHypotheses, is_prefix
from nemo.core.classes import Typing, typecheck
from nemo.core.neural_types import AcousticEncodedRepresentation, HypothesisType, LengthsType, NeuralType
from nemo.utils import logging

try:
    import kenlm

    KENLM_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    KENLM_AVAILABLE = False


class BeamTDTInfer(Typing):
    """
    Beam search implementation for Token-andDuration Transducer (TDT) models.

    Sequence level beam decoding or batched-beam decoding, performed auto-repressively
    depending on the search type chosen.

    Args:
        decoder_model: rnnt_utils.AbstractRNNTDecoder implementation.
        joint_model: rnnt_utils.AbstractRNNTJoint implementation.
        durations: list of duration values from TDT model.

        beam_size: number of beams for beam search. Must be a positive integer >= 1.
            If beam size is 1, defaults to stateful greedy search.
            For accurate greedy results, please use GreedyRNNTInfer or GreedyBatchedRNNTInfer.

        search_type: str representing the type of beam search to perform.
            Must be one of ['beam', 'maes'].

            Algoritm used:

                `default` - basic beam search strategy. Larger beams generally result in better decoding,
                    however the time required for the search also grows steadily.

                `maes` = modified adaptive expansion search. Please refer to the paper:
                    [Accelerating RNN Transducer Inference via Adaptive Expansion Search](https://ieeexplore.ieee.org/document/9250505)

                    Modified Adaptive Synchronous Decoding (mAES) execution time is adaptive w.r.t the
                    number of expansions (for tokens) required per timestep. The number of expansions can usually
                    be constrained to 1 or 2, and in most cases 2 is sufficient.

                    This beam search technique can possibly obtain superior WER while sacrificing some evaluation time.

        score_norm: bool, whether to normalize the scores of the log probabilities.

        return_best_hypothesis: bool, decides whether to return a single hypothesis (the best out of N),
            or return all N hypothesis (sorted with best score first). The container class changes based
            this flag -
            When set to True (default), returns a single Hypothesis.
            When set to False, returns a NBestHypotheses container, which contains a list of Hypothesis.

        # The following arguments are specific to the chosen `search_type`

        # mAES flags
        maes_num_steps: Number of adaptive steps to take. From the paper, 2 steps is generally sufficient. int > 1.

        maes_prefix_alpha: Maximum prefix length in prefix search. Must be an integer, and is advised to keep this as 1
            in order to reduce expensive beam search cost later. int >= 0.

        maes_expansion_beta: Maximum number of prefix expansions allowed, in addition to the beam size.
            Effectively, the number of hypothesis = beam_size + maes_expansion_beta. Must be an int >= 0,
            and affects the speed of inference since large values will perform large beam search in the next step.

        maes_expansion_gamma: Float pruning threshold used in the prune-by-value step when computing the expansions.
            The default (2.3) is selected from the paper. It performs a comparison (max_log_prob - gamma <= log_prob[v])
            where v is all vocabulary indices in the Vocab set and max_log_prob is the "most" likely token to be
            predicted. Gamma therefore provides a margin of additional tokens which can be potential candidates for
            expansion apart from the "most likely" candidate.
            Lower values will reduce the number of expansions (by increasing pruning-by-value, thereby improving speed
            but hurting accuracy). Higher values will increase the number of expansions (by reducing pruning-by-value,
            thereby reducing speed but potentially improving accuracy). This is a hyper parameter to be experimentally
            tuned on a validation set.

        softmax_temperature: Scales the logits of the joint prior to computing log_softmax.

        preserve_alignments: Bool flag which preserves the history of alignments generated during
            beam decoding (sample). When set to true, the Hypothesis will contain
            the non-null value for `alignments` in it. Here, `alignments` is a List of List of Tensor (of length V + 1).

            The length of the list corresponds to the Acoustic Length (T).
            Each value in the list (Ti) is a torch.Tensor (U), representing 1 or more targets from a vocabulary.
            U is the number of target tokens for the current timestep Ti.

            NOTE: `preserve_alignments` is an invalid argument for any `search_type`
            other than basic beam search.

        ngram_lm_model: str
            The path to the N-gram LM.
        ngram_lm_alpha: float
            Alpha weight of N-gram LM.
    """

    @property
    def input_types(self):
        """Returns definitions of module input ports."""
        return {
            "encoder_output": NeuralType(('B', 'D', 'T'), AcousticEncodedRepresentation()),
            "encoded_lengths": NeuralType(tuple('B'), LengthsType()),
            "partial_hypotheses": [NeuralType(elements_type=HypothesisType(), optional=True)],  # must always be last
        }

    @property
    def output_types(self):
        """Returns definitions of module output ports."""
        return {"predictions": [NeuralType(elements_type=HypothesisType())]}

    def __init__(
        self,
        decoder_model: rnnt_abstract.AbstractRNNTDecoder,
        joint_model: rnnt_abstract.AbstractRNNTJoint,
        durations: list,
        beam_size: int,
        search_type: str = 'default',
        score_norm: bool = True,
        return_best_hypothesis: bool = True,
        maes_num_steps: int = 2,
        maes_prefix_alpha: int = 1,
        maes_expansion_gamma: float = 2.3,
        maes_expansion_beta: int = 2,
        softmax_temperature: float = 1.0,
        preserve_alignments: bool = False,
        ngram_lm_model: Optional[str] = None,
        ngram_lm_alpha: float = 0.3,
    ):
        self.joint = joint_model
        self.decoder = decoder_model
        self.durations = durations

        self.token_offset = 0
        self.search_type = search_type
        self.blank = decoder_model.blank_idx
        self.vocab_size = decoder_model.vocab_size
        self.return_best_hypothesis = return_best_hypothesis

        self.beam_size = beam_size
        self.score_norm = score_norm
        self.max_candidates = beam_size
        self.softmax_temperature = softmax_temperature
        self.preserve_alignments = preserve_alignments

        if preserve_alignments:
            raise ValueError("Alignment preservation has not been implemented.")
        if beam_size < 1:
            raise ValueError("Beam search size cannot be less than 1!")

        if self.preserve_alignments:
            raise NotImplementedError("Preserving alignments is not implemented.")

        if search_type == "default":
            if self.beam_size == 1:
                logging.info(
                    """If beam size is 1, defaults to stateful greedy search.
                     For accurate greedy results, please use GreedyTDTInfer or GreedyBatchedTDTInfer."""
                )
            self.search_algorithm = self.default_beam_search
        elif search_type == "tsd":
            raise NotImplementedError("`tsd` (Time Synchronous Decoding) has not been implemented.")
        elif search_type == "alsd":
            raise NotImplementedError("`alsd` (Alignment Length Synchronous Decoding) has not been implemented.")
        elif search_type == "nsc":
            raise NotImplementedError("`nsc` (Constrained Beam Search) has not been implemented.")
        elif search_type == "maes":
            self.search_algorithm = self.modified_adaptive_expansion_search
        else:
            raise NotImplementedError(
                f"The search type ({search_type}) supplied is not supported!\n" f"Please use one of : (default, maes)"
            )

        if self.search_type == 'maes':
            self.maes_num_steps = int(maes_num_steps)
            self.maes_prefix_alpha = int(maes_prefix_alpha)
            self.maes_expansion_beta = int(maes_expansion_beta)
            self.maes_expansion_gamma = float(maes_expansion_gamma)

            self.max_candidates += maes_expansion_beta

            if self.maes_prefix_alpha < 0:
                raise ValueError("`maes_prefix_alpha` must be a positive integer.")

            if self.vocab_size < beam_size + maes_expansion_beta:
                raise ValueError(
                    f"beam_size ({beam_size}) + expansion_beta ({maes_expansion_beta}) "
                    f"should be smaller or equal to vocabulary size ({self.vocab_size})."
                )

            if self.maes_num_steps < 2:
                raise ValueError("`maes_num_steps` must be greater than 1.")

        try:
            self.zero_duration_idx = self.durations.index(0)
        except ValueError:
            self.zero_duration_idx = None
        self.min_non_zero_duration_idx = np.argmin(np.ma.masked_where(np.array(self.durations) == 0, self.durations))

        if ngram_lm_model:
            if search_type != "maes":
                raise ValueError("For decoding with language model `maes` decoding strategy must be chosen.")

            if KENLM_AVAILABLE:
                self.ngram_lm = kenlm.Model(ngram_lm_model)
                self.ngram_lm_alpha = ngram_lm_alpha
            else:
                raise ImportError(
                    "KenLM package (https://github.com/kpu/kenlm) is not installed. " "Use ngram_lm_model=None."
                )
        else:
            self.ngram_lm = None

    @typecheck()
    def __call__(
        self,
        encoder_output: torch.Tensor,
        encoded_lengths: torch.Tensor,
        partial_hypotheses: Optional[List[Hypothesis]] = None,
    ) -> Union[Hypothesis, NBestHypotheses]:
        """Perform general beam search.

        Args:
            encoder_output: encoder outputs (batch, features, timesteps).
            encoded_lengths: lengths of the encoder outputs.

        Returns:
            Either a list containing a single Hypothesis (when `return_best_hypothesis=True`,
            otherwise a list containing a single NBestHypotheses, which itself contains a list of
            Hypothesis. This list is sorted such that the best hypothesis is the first element.
        """
        # Preserve decoder and joint training state
        decoder_training_state = self.decoder.training
        joint_training_state = self.joint.training

        with torch.inference_mode():
            # Apply optional preprocessing
            encoder_output = encoder_output.transpose(1, 2)  # (B, T, D)

            self.decoder.eval()
            self.joint.eval()

            hypotheses = []
            with tqdm(
                range(encoder_output.size(0)),
                desc='Beam search progress:',
                total=encoder_output.size(0),
                unit='sample',
            ) as idx_gen:

                _p = next(self.joint.parameters())
                dtype = _p.dtype

                # Decode every sample in the batch independently.
                for batch_idx in idx_gen:
                    inseq = encoder_output[batch_idx : batch_idx + 1, : encoded_lengths[batch_idx], :]  # [1, T, D]
                    logitlen = encoded_lengths[batch_idx]

                    if inseq.dtype != dtype:
                        inseq = inseq.to(dtype=dtype)

                    # Extract partial hypothesis if exists
                    partial_hypothesis = partial_hypotheses[batch_idx] if partial_hypotheses is not None else None

                    # Execute the specific search strategy
                    nbest_hyps = self.search_algorithm(
                        inseq, logitlen, partial_hypotheses=partial_hypothesis
                    )  # sorted list of hypothesis

                    # Prepare the list of hypotheses
                    nbest_hyps = pack_hypotheses(nbest_hyps)

                    # Pack the result
                    if self.return_best_hypothesis:
                        best_hypothesis = nbest_hyps[0]  # type: Hypothesis
                    else:
                        best_hypothesis = NBestHypotheses(nbest_hyps)  # type: NBestHypotheses
                    hypotheses.append(best_hypothesis)

        self.decoder.train(decoder_training_state)
        self.joint.train(joint_training_state)

        return (hypotheses,)

    def greedy_search(
        self, h: torch.Tensor, encoded_lengths: torch.Tensor, partial_hypotheses: Optional[Hypothesis] = None
    ) -> List[Hypothesis]:
        """Greedy search implementation for transducer.
        Generic case when beam size = 1. Results might differ slightly due to implementation details
        as compared to `GreedyRNNTInfer` and `GreedyBatchRNNTInfer`.

        Args:
            h: Encoded speech features (1, T_max, D_enc)

        Returns:
            hyp: 1-best decoding results
        """
        logging.info(
            """If beam size is 1, defaults to stateful greedy search.
                     For accurate greedy results, please use GreedyTDTInfer or GreedyBatchedTDTInfer."""
        )
        raise NotImplementedError("greedy search has not been implemented")

    def default_beam_search(
        self,
        encoder_outputs: torch.Tensor,
        encoded_lengths: torch.Tensor,
        partial_hypotheses: Optional[Hypothesis] = None,
    ) -> List[Hypothesis]:
        """Default Beam search implementation for TDT models.

        Args:
            encoder_outputs: encoder outputs (batch, features, timesteps).
            encoded_lengths: lengths of the encoder outputs.
            partial_hypotheses: partial hypoteses.

        Returns:
            nbest_hyps: N-best decoding results
        """
        if partial_hypotheses is not None:
            raise NotImplementedError("Support for `partial_hypotheses` is not implemented.")

        beam = min(self.beam_size, self.vocab_size)
        beam_k = min(beam, (self.vocab_size - 1))
        durations_beam_k = min(beam, len(self.durations))

        # Initialize zero vector states.
        decoder_state = self.decoder.initialize_state(encoder_outputs)
        # Cache decoder results to avoid duplicate computations.
        cache = {}

        # Initialize hypothesis array with blank hypothesis.
        start_hyp = Hypothesis(
            score=0.0, y_sequence=[self.blank], dec_state=decoder_state, timestep=[-1], length=0, last_frame=0
        )
        kept_hyps = [start_hyp]

        time_idx = 0
        for time_idx in range(int(encoded_lengths)):
            # Retrieve hypotheses for current and future frames
            hyps = [hyp for hyp in kept_hyps if hyp.last_frame == time_idx]  # hypotheses for current frame
            kept_hyps = [hyp for hyp in kept_hyps if hyp.last_frame > time_idx]  # hypothesis for future frames

            # Loop over hypotheses of current frame
            while len(hyps) > 0:
                max_hyp = max(hyps, key=lambda x: x.score)
                hyps.remove(max_hyp)

                # Update decoder state and get probability distribution over vocabulary and durations.
                encoder_output = encoder_outputs[:, time_idx : time_idx + 1, :]  # [1, 1, D]
                decoder_output, decoder_state, _ = self.decoder.score_hypothesis(max_hyp, cache)  # [1, 1, D]
                logits = (
                    self.joint.joint(encoder_output, decoder_output) / self.softmax_temperature
                )  # [1, 1, 1, V + NUM_DURATIONS + 1]
                logp = torch.log_softmax(logits[0, 0, 0, : -len(self.durations)], dim=-1)  # [V + 1]
                durations_logp = torch.log_softmax(logits[0, 0, 0, -len(self.durations) :], dim=-1)  # [NUM_DURATIONS]

                # Proccess non-blank tokens
                # Retrieve the top `beam_k` most probable tokens and the top `duration_beam_k` most probable durations.
                # Then, select the top `beam_k` pairs of (token, duration) based on the highest combined probabilities.
                # Note that indices are obtained in the flattened array.
                logp_topks, logp_topk_idxs = logp[:-1].topk(beam_k, dim=-1)  # topk of tokens without blank token
                durations_logp_topks, durations_logp_topk_idxs = durations_logp.topk(durations_beam_k, dim=-1)
                total_logp_topks, total_logp_topk_idxs = (
                    torch.cartesian_prod(durations_logp_topks, logp_topks).sum(dim=-1).topk(beam_k, dim=-1)
                )

                # Loop over pairs of (token, duration) with highest combined log prob
                for total_logp_topk, total_logp_topk_idx in zip(total_logp_topks, total_logp_topk_idxs):
                    # Restore indices from flattened array indices
                    token_idx = int(logp_topk_idxs[total_logp_topk_idx % beam_k])
                    duration_idx = int(durations_logp_topk_idxs[total_logp_topk_idx // beam_k])

                    duration = self.durations[duration_idx]
                    # Construct hypothesis for non-blank token
                    new_hyp = Hypothesis(
                        score=float(max_hyp.score + total_logp_topk),  # update score
                        y_sequence=max_hyp.y_sequence + [token_idx],  # update hypothesis sequence
                        dec_state=decoder_state,  # update decoder state
                        timestep=max_hyp.timestep + [time_idx + duration],  # update timesteps
                        length=encoded_lengths,
                        last_frame=max_hyp.last_frame + duration,
                    )  # update frame idx where last token appeared

                    # Update current frame hypotheses if duration is zero and future frame hypotheses otherwise
                    if duration == 0:
                        hyps.append(new_hyp)
                    else:
                        kept_hyps.append(new_hyp)

                # Update future frames with blank tokens
                # Note: blank token can have only non-zero duration
                for duration_idx in durations_logp_topk_idxs:
                    # If zero is the only duration in topk, switch to closest non-zero duration to continue
                    if duration_idx == self.zero_duration_idx:
                        if durations_logp_topk_idxs.shape[0] == 1:
                            duration_idx = self.min_non_zero_duration_idx
                        else:
                            continue

                    duration = self.durations[int(duration_idx)]
                    new_hyp = Hypothesis(
                        score=float(max_hyp.score + logp[self.blank] + durations_logp[duration_idx]),  # update score
                        y_sequence=max_hyp.y_sequence[:],  # no need to update sequence
                        dec_state=max_hyp.dec_state,  # no need to update decoder state
                        timestep=max_hyp.timestep[:],  # no need to update timesteps
                        length=encoded_lengths,
                        last_frame=max_hyp.last_frame + duration,
                    )  # update frame idx where last token appeared
                    kept_hyps.append(new_hyp)

                # Remove duplicate hypotheses.
                # If two consecutive blank tokens are predicted and their duration values sum up to the same number,
                # it will produce two hypotheses with the same token sequence but different scores.
                kept_hyps = self.remove_duplicate_hypotheses(kept_hyps)

                if len(hyps) > 0:
                    # Keep those hypothesis that have scores greater than next search generation
                    hyps_max = float(max(hyps, key=lambda x: x.score).score)
                    kept_most_prob = sorted(
                        [hyp for hyp in kept_hyps if hyp.score > hyps_max],
                        key=lambda x: x.score,
                    )
                    # If enough hypotheses have scores greater than next search generation,
                    # stop beam search.
                    if len(kept_most_prob) >= beam:
                        kept_hyps = kept_most_prob
                        break
                else:
                    # If there are no hypotheses in a current frame, keep only `beam` best hypotheses for the next search generation.
                    kept_hyps = sorted(kept_hyps, key=lambda x: x.score, reverse=True)[:beam]
        return self.sort_nbest(kept_hyps)

    def modified_adaptive_expansion_search(
        self,
        encoder_outputs: torch.Tensor,
        encoded_lengths: torch.Tensor,
        partial_hypotheses: Optional[Hypothesis] = None,
    ) -> List[Hypothesis]:
        """
        Modified Adaptive Exoansion Search algorithm for TDT models.
        Based on/modified from https://ieeexplore.ieee.org/document/9250505.
        Supports N-gram language model shallow fusion.

        Args:
            encoder_outputs: encoder outputs (batch, features, timesteps).
            encoded_lengths: lengths of the encoder outputs.
            partial_hypotheses: partial hypotheses.

        Returns:
            nbest_hyps: N-best decoding results
        """
        if partial_hypotheses is not None:
            raise NotImplementedError("Support for `partial_hypotheses` is not implemented.")

        beam = min(self.beam_size, self.vocab_size)
        duration_beam = min(self.max_candidates, len(self.durations))

        beam_state = self.decoder.initialize_state(
            torch.zeros(beam, device=encoder_outputs.device, dtype=encoder_outputs.dtype)
        )  # [L, B, H], [L, B, H] for LSTMS

        # Initialize first hypothesis for the beam (blank).
        start_hyp = Hypothesis(
            y_sequence=[self.blank],
            score=0.0,
            dec_state=self.decoder.batch_select_state(beam_state, 0),
            timestep=[-1],
            length=0,
            last_frame=0,
        )
        init_tokens = [start_hyp]

        # Cache decoder results to avoid duplicate computations.
        cache = {}

        # Decode a batch of beam states and scores
        beam_decoder_output, beam_state, _ = self.decoder.batch_score_hypothesis(init_tokens, cache, beam_state)
        state = self.decoder.batch_select_state(beam_state, 0)

        # Setup ngram LM:
        if self.ngram_lm:
            init_lm_state = kenlm.State()
            self.ngram_lm.BeginSentenceWrite(init_lm_state)

        # Initialize first hypothesis for the beam (blank) for kept hypotheses
        start_hyp_kept = Hypothesis(
            y_sequence=[self.blank],
            score=0.0,
            dec_state=state,
            dec_out=[beam_decoder_output[0]],
            timestep=[-1],
            length=0,
            last_frame=0,
        )
        if self.ngram_lm:
            start_hyp_kept.ngram_lm_state = init_lm_state
        kept_hyps = [start_hyp_kept]

        for time_idx in range(encoded_lengths):
            # Select current iteration hypotheses
            hyps = [x for x in kept_hyps if x.last_frame == time_idx]
            kept_hyps = [x for x in kept_hyps if x.last_frame > time_idx]

            if len(hyps) == 0:
                continue

            beam_encoder_output = encoder_outputs[:, time_idx : time_idx + 1]  # [1, 1, D]

            list_b = []  # List that contains the blank token emissions
            list_nb = []  # List that contains the non-zero duration non-blank token emissions
            # Repeat for number of mAES steps
            for n in range(self.maes_num_steps):
                # Pack the decoder logits for all current hypotheses
                beam_decoder_output = torch.stack([h.dec_out[-1] for h in hyps])  # [H, 1, D]

                # Extract the log probabilities
                beam_logits = self.joint.joint(beam_encoder_output, beam_decoder_output) / self.softmax_temperature
                beam_logp = torch.log_softmax(beam_logits[:, 0, 0, : -len(self.durations)], dim=-1)
                beam_duration_logp = torch.log_softmax(beam_logits[:, 0, 0, -len(self.durations) :], dim=-1)

                # Retrieve the top `max_candidades` most probable tokens and the top `duration_beam` most probable durations.
                # Then, select the top `max_candidates` pairs of (token, duration) based on the highest combined probabilities.
                # Note that indices are obtained in flattened array.
                beam_logp_topks, beam_idx_topks = beam_logp.topk(self.max_candidates, dim=-1)
                beam_duration_logp_topks, beam_duration_idx_topks = beam_duration_logp.topk(duration_beam, dim=-1)
                beam_total_logp = (beam_duration_logp_topks[:, :, None] + beam_logp_topks[:, None, :]).view(
                    len(hyps), -1
                )  # [B, MAX_CANDIDATES*DURATION_BEAM]
                beam_total_logp_topks, beam_total_logp_topk_idxs = beam_total_logp.topk(
                    self.max_candidates, dim=-1
                )  # [B, MAX_CANDIDATES]

                # Prune hypothesis to obtain k expansions
                beam_best_expansion_scores = beam_total_logp_topks.max(dim=-1, keepdim=True).values
                beam_masks = beam_total_logp_topks >= beam_best_expansion_scores - self.maes_expansion_gamma
                beam_kexpansions_idxs = [
                    sum_logp_topk_idxs[mask] for sum_logp_topk_idxs, mask in zip(beam_total_logp_topk_idxs, beam_masks)
                ]

                list_exp = []  # List that contains the hypothesis expansion
                for hyp_idx, hyp in enumerate(hyps):  # For all hypothesis
                    # Restore indices in logp and durations_logp arrays from flattened indices.
                    hyp_expansions = [
                        (
                            int(beam_idx_topks[hyp_idx][idx % self.max_candidates]),
                            int(beam_duration_idx_topks[hyp_idx][idx // self.max_candidates]),
                            float(beam_total_logp[hyp_idx][idx]),
                        )
                        for idx in beam_kexpansions_idxs[hyp_idx]
                    ]
                    for k, duration_idx, total_logp in hyp_expansions:  # For all expansions within this hypothesis
                        # Forcing blank token to have non-zero duration
                        # Possible duplicates are removed further
                        if (
                            k == self.blank
                            and self.zero_duration_idx != None
                            and duration_idx == self.zero_duration_idx
                        ):
                            duration_idx = int(self.min_non_zero_duration_idx)

                        duration = self.durations[duration_idx]

                        new_hyp = Hypothesis(
                            score=hyp.score + total_logp,
                            y_sequence=hyp.y_sequence[:],
                            dec_out=hyp.dec_out[:],
                            dec_state=hyp.dec_state,
                            timestep=hyp.timestep[:],
                            length=time_idx,
                            last_frame=int(hyp.last_frame + duration),
                        )

                        if self.ngram_lm:
                            new_hyp.ngram_lm_state = hyp.ngram_lm_state

                        # If the expansion was for blank
                        if k == self.blank:
                            list_b.append(new_hyp)
                        else:
                            new_hyp.y_sequence.append(int(k))
                            new_hyp.timestep.append(time_idx)

                            if self.ngram_lm:
                                lm_score, new_hyp.ngram_lm_state = self.compute_ngram_score(hyp.ngram_lm_state, int(k))
                                new_hyp.score += self.ngram_lm_alpha * lm_score

                            # If token duration is 0 adding to expansions list
                            if duration == 0:
                                list_exp.append(new_hyp)
                            else:
                                list_nb.append(new_hyp)

                # Update states for hypothesis that do not end with blank
                hyps_to_update = list_nb + list_exp
                if len(hyps_to_update) > 0:
                    # Initialize the beam states for the hypotheses in the expannsion list
                    beam_state = self.decoder.batch_initialize_states(
                        beam_state, [hyp.dec_state for hyp in hyps_to_update]
                    )

                    # Decode a batch of beam states and scores
                    beam_decoder_output, beam_state, _ = self.decoder.batch_score_hypothesis(
                        hyps_to_update,
                        cache,
                        beam_state,
                    )
                    for hyp_idx, hyp in enumerate(hyps_to_update):
                        # Preserve the decoder logits for the current beam
                        hyp.dec_out.append(beam_decoder_output[hyp_idx])
                        hyp.dec_state = self.decoder.batch_select_state(beam_state, hyp_idx)

                # If there were no token expansions in any of the hypotheses,
                # Early exit
                if not list_exp:
                    kept_hyps = kept_hyps + list_b + list_nb
                    kept_hyps = self.remove_duplicate_hypotheses(kept_hyps)
                    kept_hyps = sorted(kept_hyps, key=lambda x: x.score, reverse=True)[:beam]

                    break
                else:
                    # If this isn't the last mAES step
                    if n < (self.maes_num_steps - 1):
                        # Copy the expanded hypothesis for the next iteration
                        hyps = self.remove_duplicate_hypotheses(list_exp)
                    else:
                        # If this is the last mAES step add probabilities of the blank token to the end.
                        # Extract the log probabilities
                        beam_decoder_output = torch.stack([h.dec_out[-1] for h in list_exp])  # [H, 1, D]
                        beam_logits = (
                            self.joint.joint(beam_encoder_output, beam_decoder_output) / self.softmax_temperature
                        )
                        beam_logp = torch.log_softmax(beam_logits[:, 0, 0, : -len(self.durations)], dim=-1)

                        # Get most probable durations
                        beam_duration_logp = torch.log_softmax(beam_logits[:, 0, 0, -len(self.durations) :], dim=-1)
                        _, beam_max_duration_idx = torch.max(beam_duration_logp, dim=-1)

                        # For all expansions, add the score for the blank label
                        for hyp_idx, hyp in enumerate(list_exp):
                            # If zero duration was obtained, change to the closest non-zero duration
                            duration_idx = int(beam_max_duration_idx[hyp_idx])
                            if duration_idx == self.zero_duration_idx:
                                duration_idx = self.min_non_zero_duration_idx

                            total_logp = float(
                                beam_logp[hyp_idx, self.blank] + beam_duration_logp[hyp_idx, duration_idx]
                            )
                            hyp.score += total_logp
                            hyp.last_frame += self.durations[int(duration_idx)]

                        # Finally, update the kept hypothesis of sorted top Beam candidates
                        kept_hyps = kept_hyps + list_b + list_exp + list_nb
                        kept_hyps = self.remove_duplicate_hypotheses(kept_hyps)
                        kept_hyps = sorted(kept_hyps, key=lambda x: x.score, reverse=True)[:beam]

        # Sort the hypothesis with best scores
        return self.sort_nbest(kept_hyps)

    def remove_duplicate_hypotheses(self, hypotheses):
        """
        Removes hypotheses that have identical token sequences and lengths.
        Among duplicate hypotheses, only the one with the lowest score is kept.
        Duplicate hypotheses occur when two consecutive blank tokens are predicted,
        and their duration values sum up to the same number.

        Args:
            hypotheses: list of hypotheses.

        Returns:
            hypotheses: list if hypotheses without duplicates.
        """
        sorted_hyps = sorted(hypotheses, key=lambda x: x.score, reverse=True)
        kept_hyps = []
        for hyp in sorted_hyps:
            is_present = False
            for kept_hyp in kept_hyps:
                if kept_hyp.y_sequence == hyp.y_sequence and kept_hyp.last_frame == hyp.last_frame:
                    kept_hyp.score = torch.logaddexp(torch.tensor(kept_hyp.score), torch.tensor(hyp.score))
                    is_present = True
                    break
            if not is_present:
                kept_hyps.append(hyp)
        return kept_hyps

    def set_decoding_type(self, decoding_type: str):
        # Please check train_kenlm.py in scripts/asr_language_modeling/ to find out why we need
        # TOKEN_OFFSET for BPE-based models
        if decoding_type == 'subword':
            from nemo.collections.asr.parts.submodules.ctc_beam_decoding import DEFAULT_TOKEN_OFFSET

            self.token_offset = DEFAULT_TOKEN_OFFSET

    def compute_ngram_score(self, current_lm_state: "kenlm.State", label: int) -> Tuple[float, "kenlm.State"]:
        """
        Computes the score for KenLM Ngram language model.

        Args:
            current_lm_state: current state of the KenLM language model.
            label: next label.

        Returns:
            lm_score: score for `label`.
        """
        if self.token_offset:
            label = chr(label + self.token_offset)
        else:
            label = str(label)

        next_state = kenlm.State()
        lm_score = self.ngram_lm.BaseScore(current_lm_state, label, next_state)
        lm_score *= 1.0 / np.log10(np.e)

        return lm_score, next_state

    def sort_nbest(self, hyps: List[Hypothesis]) -> List[Hypothesis]:
        """Sort hypotheses by score or score given sequence length.

        Args:
            hyps: list of hypotheses

        Return:
            hyps: sorted list of hypotheses
        """
        if self.score_norm:
            return sorted(hyps, key=lambda x: x.score / len(x.y_sequence), reverse=True)
        else:
            return sorted(hyps, key=lambda x: x.score, reverse=True)
