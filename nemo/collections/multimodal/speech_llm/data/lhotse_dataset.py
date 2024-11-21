import math
import random

import torch.utils.data
from lhotse import CutSet
from lhotse.dataset import AudioSamples
from lhotse.dataset.collation import collate_vectors as collate_vectors_lhotse

from nemo.utils import logging

from nemo.collections.multimodal.speech_llm.parts.utils.data_utils import (
    TextProcessing,
    build_loss_mask,
    ceil_to_nearest,
)


def collate_vectors(items, max_length: int, padding_value):
    vectors = collate_vectors_lhotse(items, padding_value=padding_value)
    if max_length > vectors.size(1):
        vectors = torch.cat(
            [vectors, padding_value * torch.ones(vectors.size(0), max_length - vectors.size(1), dtype=vectors.dtype)],
            dim=1,
        )
    if items[0].shape[0] < 1:
        vectors = vectors.long()
    return vectors


# TODO: the changes in this file needed to be moved out as a derived class
class LhotseAudioQuestionAnswerDataset(torch.utils.data.Dataset):
    """
    This dataset is based on Lhotse ASR dataset from ``audio_to_text_lhotse.py``
    and ``TarredAudioQuestionAnswerDataset`` from ``audio_text_qa_dataset.py``.

    Unlike native NeMo datasets, Lhotse dataset defines only the mapping from
    a CutSet (meta-data) to a mini-batch with PyTorch tensors.
    Specifically, it performs tokenization, I/O, augmentation, and feature extraction (if any).
    Managing data, sampling, de-duplication across workers/nodes etc. is all handled
    by Lhotse samplers instead.

    Args:
        text_processor: TextProcessing object
        default_context: Default question to use if no question is provided
        tokens_to_generate: Number of tokens to generate during inference
        pad_to_max_length: Whether to pad the input to the max sequence length. If False, will pad to the max length of the current batch.
        max_seq_length: Maximum sequence length for each dataset examples. Examples will either be truncated to fit this length or dropped if they cannot be truncated.
        context_key: Key to use for the context in your JSONL file
        default_context_key: Key to use for the default context in lhotse yaml
    """

    def __init__(
        self,
        text_processor: TextProcessing,
        default_context: str,
        tokens_to_generate: int,
        pad_to_max_length: bool,
        max_seq_length: int,
        context_key: str = "context",
        default_context_key: str = "default_context",
        vocab_sizes: list[int] = [-1],
        decoder_reduction_factor: int = 1,
        speech_pad_id: int = 1001,
        speech_unk_id: int = 1002,
        speech_bos_id: int = 1003,
        speech_eos_id: int = 1004,
        filter_by_source_target_text_ratio: bool = False,
        source_target_text_ratio_limit: float = 1.0,
        sample_rate: int = 22050,
        t5_style: bool = False,
        load_answer_audio: bool = False,
        codec_model_downsampling_factor: float = 1023.5,
    ):
        super().__init__()
        self.text_processor = text_processor
        self.load_audio = AudioSamples(fault_tolerant=True)
        self.tokens_to_generate = tokens_to_generate
        self.pad_to_max_length = pad_to_max_length
        self.max_seq_length = max_seq_length

        self.default_context = default_context
        self.context_key = context_key
        self.default_context_key = default_context_key

        if len(vocab_sizes) == 1 and vocab_sizes[0] <= 0:
            vocab_sizes = [self.text_processor.tokenizer.vocab_size]
        self.vocab_sizes = list(vocab_sizes)
        self.n_speech_codebooks = len(self.vocab_sizes) - 1
        self.decoder_reduction_factor = decoder_reduction_factor
        self.speech_pad_id = speech_pad_id
        self.speech_unk_id = speech_unk_id
        self.speech_bos_id = speech_bos_id
        self.speech_eos_id = speech_eos_id
        self.filter_by_source_target_text_ratio = filter_by_source_target_text_ratio
        self.source_target_text_ratio_limit = source_target_text_ratio_limit
        self.sample_rate = sample_rate
        self.load_answer_audio = load_answer_audio
        self.codec_model_downsampling_factor = codec_model_downsampling_factor

        # To be consistent with SALM text processor
        self.text_processor.add_sep = False
        self.text_processor.max_seq_length = (
            4096  # Set this to a large number for since the speech sequence can be long
        )
        self.t5_style = t5_style

    def __getitem__(self, cuts) -> dict[str, torch.Tensor | list[str] | dict]:
        import re

        def discretize_time(start_token, speech_resolution=0.08, timestamp_resolution=0.08):
            """Convert the start token into a time index based on the resolution."""
            return int(start_token * timestamp_resolution / speech_resolution)

        def extract_text_and_time_tokens(input_sequence):
            # Regular expression to match time tokens (e.g., <|x|> where x is an integer)
            time_token_pattern = r"<\|(\d+)\|>"
            # Find all time tokens
            time_tokens = re.findall(time_token_pattern, input_sequence)
            # Only keep the first token of every pair (i.e., start time tokens)
            start_time_token = [int(time_tokens[i]) for i in range(0, len(time_tokens), 2)]
            # Remove all time tokens to isolate words
            words = re.sub(time_token_pattern, '', input_sequence).split()
            # Process each word, tokenize it, and calculate token lengths
            tokenized_words = []
            word_length = []
            for idx, word in enumerate(words):
                # Tokenize the word using the provided text processor
                tokenized_word = self.text_processor._process_example(context="", output=word)
                # Remove the EOS token (assuming the EOS token is at the end of "answer_ids")
                token_ids = tokenized_word["answer_ids"][:-1]  # Remove EOS token
                if idx != 0:  # If not the first word, remove the first token
                    token_ids = token_ids[1:]
                token_length = len(token_ids)  # Calculate the length
                tokenized_words.extend(token_ids)
                word_length.append(token_length)
            return (
                torch.as_tensor(tokenized_words),
                torch.as_tensor(start_time_token),
                torch.as_tensor(word_length),
            )

        def _expand_text_with_timestamps_and_word_lengths(
            word_tokens, word_lengths, start_time_tokens, features_lens, frame_rate=0.08, pad_id=None
        ):
            """
            Expand word tokens according to start time tokens and word lengths for a batch of sequences.

            Args:
            - word_tokens: List of lists of token sequences (each inner list is a word's token IDs), shape [batch][time].
            - word_lengths: List of lists of word lengths, shape [batch][time].
            - start_time_tokens: List of lists of start times, shape [batch][time].
            - max_length: Maximum length in the time dimension (number of frames).
            - frame_rate: Frame rate resolution.
            - pad_id: Padding ID to use for empty positions in the tensor.

            Returns:
            - 2D tensor [batch, max_length] where each row is the expanded token sequence for that batch.
            """
            if pad_id is None:
                raise ValueError("pad_id must be provided.")

            batch_size = len(word_tokens)
            max_length = max(features_lens).item()

            # Create the empty 2D tensor [batch, max_length] with pad_id as the default value
            texts_expanded = torch.full((batch_size, max_length), fill_value=pad_id, dtype=torch.long)

            # Iterate over each batch
            for batch_idx in range(batch_size):
                # Remove the speech eos
                batch_max_length = features_lens[batch_idx] - 1
                word_start_idx = 0  # Start index to keep track of the position within the concatenated word tokens

                # Iterate over the words in the current batch
                for word_idx, word_length in enumerate(word_lengths[batch_idx]):
                    start_token = start_time_tokens[batch_idx][word_idx]

                    # Convert the start time token into a time index based on frame rate
                    start_time_index = discretize_time(start_token, frame_rate)

                    # Reduction of start time index due to stacking of frames
                    start_time_index = int(start_time_index / self.decoder_reduction_factor)

                    # Calculate the end time index based on word length
                    end_time_index = start_time_index + word_length
                    end_time_index = min(end_time_index, max_length)  # Ensure it doesn't exceed max length

                    # Get the word tokens for the current word
                    word_token_ids = word_tokens[batch_idx][word_start_idx : word_start_idx + word_length]

                    # Populate the tokens in the expanded tensor at the correct positions
                    for t_idx in range(start_time_index, end_time_index):
                        if t_idx - start_time_index < len(word_token_ids):  # Ensure tokens are within bounds
                            token_id = word_token_ids[t_idx - start_time_index]  # Get token for this time step
                            texts_expanded[batch_idx][t_idx] = token_id  # Directly assign the token ID

                    # Move to the next word in the concatenated word tokens
                    word_start_idx += word_length

                # Overwrite padding tokens
                texts_expanded[batch_idx][batch_max_length:] = text_pad_id

            return texts_expanded

        cuts = cuts.sort_by_duration()

        metadata = []
        instructions, instruction_lengths = [], []
        target_texts, target_text_lengths = [], []
        remove_ids = []
        start_time_tokens, word_lengths = [], []
        for id, cut in enumerate(cuts):
            metadata.append({'audio_filepath': cut.id + '.wav'})
            # TODO: the following use of _process_example is not ideal. Should update
            if cut.supervisions[0].speaker == "user":
                instruction = self.text_processor._process_example(context=cut.supervisions[0].text, output="")
                instruction, instruction_length = torch.as_tensor(instruction["input_ids"][:-1]), torch.as_tensor(
                    len(instruction["input_ids"]) - 1
                )
            else:
                raise Exception("First speaker should be user")

            if cut.supervisions[1].speaker == "agent":
                use_timestamp = getattr(cut, "s2s_align", False)
                text = cut.supervisions[1].text
                if not use_timestamp:
                    pattern = r"<\|\d+\|>"
                    output_text = re.sub(pattern, "", text)
                    output_text = re.sub(r'\s+', ' ', output_text).strip()
                    target_text = self.text_processor._process_example(context="", output=output_text)
                    # -1 to remove the eos token added by the text processor
                    target_text, target_text_length = torch.as_tensor(target_text["answer_ids"][:-1]), torch.as_tensor(
                        len(target_text["answer_ids"]) - 1
                    )
                else:
                    target_text, start_time_token, word_length = extract_text_and_time_tokens(text)
                    target_text_length = len(target_text)
            else:
                raise Exception("Second speaker should be agent")

            instructions.append(instruction)
            instruction_lengths.append(instruction_length)
            target_texts.append(target_text)
            target_text_lengths.append(target_text_length)
            if use_timestamp:
                word_lengths.append(word_length)
                start_time_tokens.append(start_time_token)

        # Load source audio
        audio = [cut.resample(self.sample_rate).load_audio() for cut in cuts]
        audio_lens = [torch.tensor(a.shape[1]).long() for a in audio]

        # Resample audio waveform here since cuts.resample causes core dump sometimes
        # cuts_sample_rates = [c.recording.sampling_rate for c in cuts]
        # import torchaudio
        # audio = [torchaudio.functional.resample(a, orig_sample_rate, self.sample_rate).squeeze(0) for a, orig_sample_rate in zip(audio, cuts_sample_rates)]
        # audio_lens = (torch.IntTensor(audio_lens) * (self.sample_rate / torch.IntTensor(cuts_sample_rates))).int()
        audio = collate_vectors([a.squeeze(0) for a in audio], max_length=max(audio_lens), padding_value=0.0)
        audio_lens = torch.tensor(audio_lens).long()

        audio_ratio = []
        for id, cut in enumerate(cuts):
            audio_ratio.append(1.0)

        for _, cut in enumerate(cuts):
            if hasattr(cut, self.context_key):
                cut.context = getattr(cut, self.context_key)
            elif hasattr(cut, self.default_context_key):
                cut.context = getattr(cut, self.default_context_key)
            else:
                cut.context = self.default_context

        text_pad_id = self.text_processor.pad_id
        text_unk_id = self.text_processor.unk_id
        text_bos_id = self.text_processor.bos_id
        text_eos_id = self.text_processor.eos_id

        def get_3d_empty_tensor(batch_size, length, text_fill_id, speech_fill_id):
            return torch.cat(
                [
                    torch.full((batch_size, length, 1), text_fill_id),
                    torch.full(
                        (batch_size, length, self.n_speech_codebooks * self.decoder_reduction_factor), speech_fill_id
                    ),
                ],
                axis=2,
            )

        def collate_and_pad(inputs):
            token_lengths = [len(seq) for seq in inputs]
            max_length = max(token_lengths)
            assert len(inputs[0].shape) < 3
            if len(inputs[0].shape) < 2:
                if self.pad_to_max_length:
                    max_length = self.max_seq_length
                else:
                    max_length = min(self.max_seq_length, ceil_to_nearest(max_length, 8))

                tokens = collate_vectors(inputs, max_length=max_length, padding_value=text_pad_id)
            else:
                tokens = get_3d_empty_tensor(len(inputs), max_length, text_pad_id, self.speech_pad_id)
                for i in range(len(tokens)):
                    tokens[i, : token_lengths[i], :] = inputs[i]
            return tokens, torch.LongTensor(token_lengths)

        target_codec = None
        answer_audios, answer_audio_lens = None, None
        assert self.load_answer_audio
        if not self.load_answer_audio:
            assert not getattr(cut, "s2t", False), "s2t not supported when load_answer_audio is False"
            features_lens = torch.tensor(
                [cut.target_codes.shape[0] // self.decoder_reduction_factor for cut in cuts], dtype=torch.int
            )
            # +1 for the eos tensor
            target_codec = get_3d_empty_tensor(
                len(cuts), max(features_lens).item() + 1, text_pad_id, self.speech_pad_id
            )
            eos_tensor = torch.full(
                (1, self.n_speech_codebooks * self.decoder_reduction_factor + 1), self.speech_eos_id
            ).to(torch.int)
            eos_tensor[:, 0] = self.text_processor.unk_id
            # Loop through cuts and build target_codec
            for i, cut in enumerate(cuts):
                feat_i = cut.target_codes.load()
                target_codec[i, : feat_i.shape[0], 0] = text_unk_id
                feat_i = feat_i[: features_lens[i] * self.decoder_reduction_factor, : self.n_speech_codebooks]
                feat_i = feat_i.reshape((-1, self.n_speech_codebooks * self.decoder_reduction_factor))
                target_codec[i, : feat_i.shape[0], 1:] = torch.tensor(feat_i)
                target_codec[i, feat_i.shape[0], :] = eos_tensor

            target_codec = target_codec.to(torch.int)
        else:
            assert not getattr(cut, "direct_s2s", False), "direct_s2s not supported when load_answer_audio is True"
            assert self.decoder_reduction_factor == 1, "TODO: add the support in on the fly"
            # TODO(subhankarg) load answer audio from cut.target_codes logic
            answer_audio_lens = []
            answer_audios = []
            features_lens = []
            for i, cut in enumerate(cuts):
                answer_audio = torch.tensor(cut.target_audio.load_audio()).float()
                answer_audio_len = torch.tensor(answer_audio.shape[1]).long()
                answer_audios.append(answer_audio)
                answer_audio_lens.append(answer_audio_len)
                features_lens.append(math.ceil(answer_audio_len / self.codec_model_downsampling_factor))
            answer_audios = collate_vectors(
                [a.squeeze(0) for a in answer_audios], max_length=max(answer_audio_lens), padding_value=0.0
            ).float()
            answer_audio_lens = torch.tensor(answer_audio_lens).long()
            # Prepare dummy target_codec with speech_pad_id and eos_tensor, the dummy values will be filled in training_step or validation_step
            # once the audio codecs are extracted from the audio.
            features_lens = torch.tensor(features_lens, dtype=torch.int)
            # TODO: can remove the following except features_lens
            target_codec = get_3d_empty_tensor(
                len(cuts), max(features_lens).item() + 1, text_pad_id, self.speech_pad_id
            )
            eos_tensor = torch.full((1, target_codec.shape[-1]), self.speech_eos_id).to(torch.int)
            eos_tensor[:, 0] = self.text_processor.unk_id
            for i, cut in enumerate(cuts):
                target_codec[i, : features_lens[i], 0] = text_unk_id
                feat_i = torch.full((features_lens[i], target_codec.shape[-1] - 1), self.speech_pad_id - 1)
                target_codec[i, : feat_i.shape[0], 1:] = feat_i
                target_codec[i, feat_i.shape[0], :] = eos_tensor
            target_codec = target_codec.to(torch.int)

        def _convert_text_to_3d_tensor(texts, include_eos=True, tokens_to_generate=0):
            texts, text_lengths = collate_and_pad(texts)
            texts_expanded = get_3d_empty_tensor(
                texts.shape[0], texts.shape[1] + 1 + tokens_to_generate, text_pad_id, self.speech_pad_id
            )
            for i, text_length in enumerate(text_lengths):
                texts_expanded[i, :text_length, 0] = texts[i, :text_length]
                texts_expanded[i, :text_length, 1:] = self.speech_unk_id
                eos_tensor = torch.full(
                    (1, self.n_speech_codebooks * self.decoder_reduction_factor + 1), self.speech_bos_id
                ).to(torch.int)
                eos_tensor[:, 0] = self.text_processor.eos_id

                texts_expanded[i, text_length, :] = eos_tensor
            if not include_eos:
                texts_expanded = texts_expanded[:, :-1]
            return texts, text_lengths, texts_expanded

        unpadded_target_texts = target_texts
        target_texts, target_text_lengths, target_texts_expanded = _convert_text_to_3d_tensor(target_texts)
        instructions, instruction_lengths, instructions_expanded_no_eos = _convert_text_to_3d_tensor(
            # tokens_to_generate is used in inference
            instructions,
            include_eos=False,
            tokens_to_generate=self.tokens_to_generate,
        )

        # answers = torch.concat([speaker_context, bos_tensor, target_codec], 1)

        # TODO: remove the following stanza
        if getattr(cut, "s2s", False):
            # Add 1 for eos token
            token_list = [
                torch.concat([tt[: ttl + 1], tc[: tcl + 1]], 0)
                for tt, ttl, tc, tcl in zip(target_texts_expanded, target_text_lengths, target_codec, features_lens)
            ]
            if not self.t5_style:
                token_list = [
                    torch.concat([it[:itl], tt], 0)
                    for tt, it, itl in zip(token_list, instructions_expanded_no_eos, instruction_lengths)
                ]
            tokens, _ = collate_and_pad(token_list)

            # speech_loss_mask = torch.logical_and((tokens[:, :, 1:] != self.speech_unk_id), (tokens[:, :, 1:] != self.speech_pad_id))
            # text_loss_mask = torch.logical_and((tokens[:, :, 0:1] != text_unk_id), (tokens[:, :, 0:1] != text_pad_id))
            speech_loss_mask = tokens[:, :, 1:] != self.speech_pad_id
            text_loss_mask = tokens[:, :, 0:1] != text_pad_id
            if not self.t5_style:
                for itl in instruction_lengths:
                    speech_loss_mask[:, :itl, :] = False
                    text_loss_mask[:, :itl, :] = False
            loss_mask = torch.cat([text_loss_mask, speech_loss_mask], 2)
            full_lengths = target_text_lengths + 1 + features_lens + 1 + instruction_lengths

        elif getattr(cut, "s2s_align", False):
            bos_tensor = torch.full(
                (target_codec.shape[0], 1, self.n_speech_codebooks * self.decoder_reduction_factor + 1),
                self.speech_bos_id,
            ).to(torch.int)

            bos_tensor[:, :, 0] = self.text_processor.bos_id
            # [batch, max_feat_len]
            # the only thing needed is features_lens which can be estimated from target_audio length
            target_texts_expanded = _expand_text_with_timestamps_and_word_lengths(
                unpadded_target_texts,
                word_lengths,
                start_time_tokens,
                features_lens + 1,
                self.codec_model_downsampling_factor / self.sample_rate,
                pad_id=text_unk_id,
            )
            # [batch, max_feat_len, 1+V], where V = #codebooks * reduction_factor
            target_codec[:, :, 0] = target_texts_expanded
            token_list = torch.concat([bos_tensor, target_codec], 1)
            features_lens += 1

            if not self.t5_style:
                token_list = [
                    torch.concat([it[:itl], tt], 0)
                    for tt, it, itl in zip(token_list, instructions_expanded_no_eos, instruction_lengths)
                ]
            tokens, _ = collate_and_pad(token_list)
            speech_loss_mask = tokens[:, :, 1:] != self.speech_pad_id
            # Make the text loss mask the same as speech since they are aligned
            loss_mask = torch.cat([speech_loss_mask[..., :1], speech_loss_mask], dim=-1)
            if not self.t5_style:
                for itl in instruction_lengths:
                    loss_mask[:, :itl, :] = False
            full_lengths = features_lens + 1 + instruction_lengths
            target_text_lengths = -1 * torch.ones_like(target_text_lengths)
        elif getattr(cut, "direct_s2s", False):
            # Add 1 for eos token
            # tt[0] is the bos token
            token_list = [
                torch.concat([tt[:1], tc[: tcl + 1]], 0)
                for tt, tc, tcl in zip(target_texts_expanded, target_codec, features_lens)
            ]
            if not self.t5_style:
                token_list = [
                    torch.concat([it[:itl], tt], 0)
                    for tt, it, itl in zip(token_list, instructions_expanded_no_eos, instruction_lengths)
                ]
            tokens, _ = collate_and_pad(token_list)

            speech_loss_mask = tokens[:, :, 1:] != self.speech_pad_id
            text_loss_mask = tokens[:, :, 0:1] != text_pad_id
            if not self.t5_style:
                for itl in instruction_lengths:
                    speech_loss_mask[:, :itl, :] = False
                    text_loss_mask[:, :itl, :] = False
            loss_mask = torch.cat([text_loss_mask, speech_loss_mask], 2)
            full_lengths = 1 + features_lens + 1 + instruction_length
        elif getattr(cut, "s2t", False):
            # Add 1 for eos token
            token_list = [tt[: ttl + 1] for tt, ttl in zip(target_texts_expanded, target_text_lengths)]
            if not self.t5_style:
                token_list = [
                    torch.concat([it[:itl], tt], 0)
                    for tt, it, itl in zip(token_list, instructions_expanded_no_eos, instruction_lengths)
                ]
            tokens, _ = collate_and_pad(token_list)

            speech_loss_mask = torch.zeros(tokens.shape[0], tokens.shape[1] - 1, tokens.shape[2])
            text_loss_mask = tokens[:, :, 0:1] != text_pad_id
            if not self.t5_style:
                for itl in instruction_lengths:
                    speech_loss_mask[:, :itl, :] = False
                    text_loss_mask[:, :itl, :] = False
            loss_mask = torch.cat([text_loss_mask, speech_loss_mask], 2)
            full_lengths = target_text_lengths + 1 + instruction_length
        full_lengths = torch.clamp(full_lengths, max=tokens.shape[1])
        # simplify above code
        # Start from index 1 since the first token will not be used as a label
        loss_mask = loss_mask[:, 1:, :]

        # Merge batch
        # note: the codec id in labels and contexts and others do not consider the offset e.g. speech_eos is 1002
        # the offset is all considered by SumVocabParallelEmbedding
        return_batch = {
            "sample_ids": list(cuts.ids),
            "audio_signal": audio,
            "audio_signal_length": audio_lens,
            "audio_ratio": torch.FloatTensor(audio_ratio),
            "metadata": metadata,
            # For forward
            "instructions": instructions,
            "target_texts_expanded": target_texts_expanded,  # used in prepare_llm_input
            "contexts": instructions_expanded_no_eos,  # used in inference
            "context_lengths": instruction_lengths,
            "tokens": tokens[:, :-1, :],
            "tokens_length": full_lengths - 1,
            "labels": tokens[:, 1:, :],
            "loss_mask": loss_mask,
            # For validation mainly
            "target_texts": target_texts,
            "target_text_lengths": target_text_lengths,
            "answers": tokens[:, 1:, :],
            "answer_audio": answer_audios,
            "answer_audio_lens": answer_audio_lens,
        }

        return return_batch


def collate_text_data(
    cuts,
    default_context: str,
    text_processor: TextProcessing,
    tokens_to_generate: int,
    pad_to_max_length: bool,
    max_seq_length: int,
) -> dict:
    """Perform text collation equivalent to nemo/collections/multimodal/data/audio_text_qa_dataset.py:121"""
    batch_size = len(cuts)
    pad_id = text_processor.pad_id
    examples = [
        {
            k: torch.as_tensor(v)
            for k, v in text_processor._process_example(
                context=cut.context,
                output=cut.supervisions[0].text if cut.supervisions[0].text is not None else "",
            ).items()
        }
        for cut in cuts
    ]
    fields = as_dict(examples)

    def get_max_len(input_list):
        return max([len(x) for x in input_list])

    max_length = tokens_to_generate + max(
        get_max_len(fields["input_ids"]), get_max_len(fields["context_ids"]), get_max_len(fields["answer_ids"])
    )
    # increase max length to nearest multiple of 4 or 8
    if pad_to_max_length:
        max_length = max_seq_length
    else:
        max_length = min(max_seq_length, ceil_to_nearest(max_length, 8))

    all_tokens = collate_vectors(fields["input_ids"], max_length=max_length, padding_value=pad_id)
    full_lengths = torch.LongTensor([len(item) for item in fields["input_ids"]])

    assert max_length <= max_seq_length, f"{max_length=} <= {max_seq_length=}"

    return {
        "tokens": all_tokens[:, :-1],
        "tokens_length": full_lengths - 1,
        "labels": all_tokens[:, 1:],
        "loss_mask": collate_vectors(
            [torch.as_tensor(build_loss_mask(item)) for item in examples], max_length=max_length, padding_value=0
        )[:, 1:],
        "position_ids": torch.arange(max_length, dtype=torch.long).repeat(batch_size, 1),
        "contexts": collate_vectors(fields["context_ids"], max_length=max_length, padding_value=pad_id),
        "context_lengths": torch.LongTensor([len(seq) for seq in fields["context_ids"]]),
        "answers": collate_vectors(fields["answer_ids"], max_length=max_length, padding_value=pad_id),
        "max_length": torch.LongTensor([max_length] * batch_size),
        "context_ids": fields["context_ids"],
    }


def as_dict(arg: list[dict]) -> dict[str, list]:
    return {k: [item[k] for item in arg] for k in arg[0].keys()}
