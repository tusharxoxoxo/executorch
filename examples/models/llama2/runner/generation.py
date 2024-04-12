# Copyright (c) Meta Platforms, Inc. and affiliates.

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple, TypedDict

import torch
import torch.nn.functional as F

from executorch.examples.models.llama2.llama_transformer import Transformer
from executorch.examples.models.llama2.tokenizer.tiktoken import (
    ChatFormat,
    Dialog,
    Message,
    Tokenizer,
)
from executorch.extension.pybindings.portable_lib import _load_for_executorch
from executorch.examples.models.llama2.llama_transformer import ModelArgs


class CompletionPrediction(TypedDict, total=False):
    generation: str
    tokens: List[str]  # not required
    logprobs: List[float]  # not required


class ChatPrediction(TypedDict, total=False):
    generation: Message
    tokens: List[str]  # not required
    logprobs: List[float]  # not required

def sample_top_p(probs, p):
    """
    Perform top-p (nucleus) sampling on a probability distribution.

    Args:
        probs (torch.Tensor): Probability distribution tensor.
        p (float): Probability threshold for top-p sampling.

    Returns:
        torch.Tensor: Sampled token indices.

    Note:
        Top-p sampling selects the smallest set of tokens whose cumulative probability mass
        exceeds the threshold p. The distribution is renormalized based on the selected tokens.
    """
    probs_sort, probs_idx = torch.sort(probs, dim=-1, descending=True)
    probs_sum = torch.cumsum(probs_sort, dim=-1)
    mask = probs_sum - probs_sort > p
    probs_sort[mask] = 0.0
    probs_sort.div_(probs_sort.sum(dim=-1, keepdim=True))
    next_token = torch.multinomial(probs_sort, num_samples=1)
    next_token = torch.gather(probs_idx, -1, next_token)
    return next_token


class LlamaRunner:
    def __init__(self, model_path: str, model_args: ModelArgs, tokenizer_path: str):
        # model is a pte file.
        self.model = _load_for_executorch(model_path)
        self.params = model_args
        self.tokenizer = Tokenizer(tokenizer_path)
        assert model_args.vocab_size == self.tokenizer.n_words

    def generate(
        self,
        prompt_tokens: List[List[int]],
        max_gen_len: int,
        temperature: float = 0.6,
        top_p: float = 0.9,
        use_kv_cache = False,
        logprobs: bool = False,
        echo: bool = False,
    ) -> Tuple[List[List[int]], Optional[List[List[float]]]]:
        bsz = len(prompt_tokens)
        params = self.params
        print(f"Params: {params}")
        assert bsz <= params.max_batch_size, (bsz, params.max_batch_size)

        min_prompt_len = min(len(t) for t in prompt_tokens)
        max_prompt_len = max(len(t) for t in prompt_tokens)

        print(f"min {min_prompt_len}, max {max_prompt_len}")

        assert max_prompt_len <= params.max_seq_len
        total_len = min(params.max_seq_len, max_gen_len + max_prompt_len)
        pad_id = self.tokenizer.pad_id
        tokens = torch.full((bsz, total_len), pad_id, dtype=torch.long, device="cpu")
        print(f"starting tokens: {tokens.shape}, tokens {tokens}")
        num_prompt_tokens = len(prompt_tokens[0])
        for k, t in enumerate(prompt_tokens):
            tokens[k, : len(t)] = torch.tensor(t, dtype=torch.long, device="cpu")
        if logprobs:
            token_logprobs = torch.zeros_like(tokens, dtype=torch.float)

        prev_pos = 0
        if use_kv_cache:
            min_prompt_len = 1

        eos_reached = torch.tensor([False] * bsz, device="cpu")
        input_text_mask = tokens != pad_id
        print(f"input_text_mask: {input_text_mask.shape}, {input_text_mask}")
        if min_prompt_len == total_len:
            if use_kv_cache:
                inputs = (tokens, prev_pos)
            else:
                inputs = (tokens,)
            logits = self.model.forward(inputs) # updated forward call.
            logits = logits[0]
            token_logprobs = -F.cross_entropy(
                input=logits.transpose(1, 2),
                target=tokens,
                reduction="none",
                ignore_index=pad_id,
            )

        stop_tokens = torch.tensor(list(self.tokenizer.stop_tokens))

        for cur_pos in range(min_prompt_len, total_len):
            print(f"prev_pos: {prev_pos}, cur_pos {cur_pos}")
            pos = torch.tensor([prev_pos], dtype=torch.int64)
            input_tok = tokens[:, prev_pos:cur_pos]
            # print(f"input tok {input_tok}, decoded {self.tokenizer.decode([input_tok])}")
            if use_kv_cache:
                inputs = (tokens[:, prev_pos:cur_pos], pos)
            else:
                inputs = (tokens[:, :cur_pos],)
            logits = self.model.forward(inputs) # Update forward call.
            print(f"logits: {logits[0].shape}, logits: {logits}")
            logits = logits[0]
            if temperature > 0:
                probs = torch.softmax(logits[:, -1] / temperature, dim=-1)
                next_token = sample_top_p(probs, top_p)
            else:
                next_token = torch.argmax(logits[:, -1], dim=-1)
            print(f"{cur_pos}: next token: {next_token}, decoded: {self.tokenizer.decode([next_token])}")

            next_token = next_token.reshape(-1)

            # only replace token if prompt has already been generated
            # set this if i < len(prompt)
            if not use_kv_cache or cur_pos < num_prompt_tokens:
                print(f"replace next_token with prompt")
                next_token = torch.where(
                    input_text_mask[:, cur_pos], tokens[:, cur_pos], next_token
                )

            tokens[:, cur_pos] = next_token
            print(f"tokens: {tokens.shape}, tokens {tokens}")

            if logprobs:
                token_logprobs[:, prev_pos + 1 : cur_pos + 1] = -F.cross_entropy(
                    input=logits.transpose(1, 2),
                    target=tokens[:, prev_pos + 1 : cur_pos + 1],
                    reduction="none",
                    ignore_index=pad_id,
                )
            eos_reached |= (~input_text_mask[:, cur_pos]) & (
                torch.isin(next_token, stop_tokens)
            )
            prev_pos = cur_pos
            if all(eos_reached):
                break

        if logprobs:
            token_logprobs = token_logprobs.tolist()
        out_tokens, out_logprobs = [], []
        for i, toks in enumerate(tokens.tolist()):
            # cut to max gen len
            start = 0 if echo else len(prompt_tokens[i])
            toks = toks[start : len(prompt_tokens[i]) + max_gen_len]
            probs = None
            if logprobs:
                probs = token_logprobs[i][start : len(prompt_tokens[i]) + max_gen_len]
            # cut to after eos tok if any
            for stop_token in self.tokenizer.stop_tokens:
                try:
                    eos_idx = toks.index(stop_token)
                    toks = toks[:eos_idx]
                    probs = probs[:eos_idx] if logprobs else None
                except ValueError:
                    pass
            out_tokens.append(toks)
            out_logprobs.append(probs)
        return (out_tokens, out_logprobs if logprobs else None)


    def text_completion(
        self,
        prompts: List[str],
        temperature: float = 0, # change this back to 0.6 later
        top_p: float = 0.9,
        max_gen_len: Optional[int] = None,
        logprobs: bool = False,
        echo: bool = False,
        use_kv_cache = False,
    ) -> List[CompletionPrediction]:
        """
        Perform text completion for a list of prompts using the language generation model.

        Args:
            prompts (List[str]): List of text prompts for completion.
            temperature (float, optional): Temperature value for controlling randomness in sampling. Defaults to 0.6.
            top_p (float, optional): Top-p probability threshold for nucleus sampling. Defaults to 0.9.
            max_gen_len (Optional[int], optional): Maximum length of the generated completion sequence.
                If not provided, it's set to the model's maximum sequence length minus 1.
            logprobs (bool, optional): Flag indicating whether to compute token log probabilities. Defaults to False.
            echo (bool, optional): Flag indicating whether to include prompt tokens in the generated output. Defaults to False.

        Returns:
            List[CompletionPrediction]: List of completion predictions, each containing the generated text completion.

        Note:
            This method generates text completions for the provided prompts, employing nucleus sampling to introduce controlled randomness.
            If logprobs is True, token log probabilities are computed for each generated token.
        """
        if max_gen_len is None:
            max_gen_len = self.model.params.max_seq_len - 1
        prompt_tokens = [self.tokenizer.encode(x, bos=True, eos=False) for x in prompts]
        print(f"text_completion: prompt_tokens: {prompt_tokens}")

        generation_tokens, generation_logprobs = self.generate(
            prompt_tokens=prompt_tokens,
            max_gen_len=max_gen_len,
            temperature=temperature,
            top_p=top_p,
            logprobs=logprobs,
            echo=echo,
            use_kv_cache = use_kv_cache,
        )
        print(f"text_completion: generation_tokens: {generation_tokens}")
        for t in generation_tokens:
            print(f"decoded: {self.tokenizer.decode(t)}")
        if logprobs:
            return [
                {
                    "generation": self.tokenizer.decode(t),
                    "tokens": [self.tokenizer.decode([x]) for x in t],
                    "logprobs": logprobs_i,
                }
                for t, logprobs_i in zip(generation_tokens, generation_logprobs)
            ]
        return [{"generation": self.tokenizer.decode(t)} for t in generation_tokens]

    def chat_completion(
        self,
        dialogs: List[Dialog],
        temperature: float = 0.6,
        top_p: float = 0.9,
        max_gen_len: Optional[int] = None,
        logprobs: bool = False,
    ) -> List[ChatPrediction]:
        """
        Generate assistant responses for a list of conversational dialogs using the language generation model.

        Args:
            dialogs (List[Dialog]): List of conversational dialogs, where each dialog is a list of messages.
            temperature (float, optional): Temperature value for controlling randomness in sampling. Defaults to 0.6.
            top_p (float, optional): Top-p probability threshold for nucleus sampling. Defaults to 0.9.
            max_gen_len (Optional[int], optional): Maximum length of the generated response sequence.
                If not provided, it's set to the model's maximum sequence length minus 1.
            logprobs (bool, optional): Flag indicating whether to compute token log probabilities. Defaults to False.

        Returns:
            List[ChatPrediction]: List of chat predictions, each containing the assistant's generated response.

        Raises:
            AssertionError: If the last message in a dialog is not from the user.
            AssertionError: If the dialog roles are not in the required 'user', 'assistant', and optional 'system' order.

        Note:
            This method generates assistant responses for the provided conversational dialogs.
            It employs nucleus sampling to introduce controlled randomness in text generation.
            If logprobs is True, token log probabilities are computed for each generated token.
        """
        if max_gen_len is None:
            max_gen_len = self.model.params.max_seq_len - 1

        prompt_tokens = [
            self.formatter.encode_dialog_prompt(dialog) for dialog in dialogs
        ]
        generation_tokens, generation_logprobs = self.generate(
            prompt_tokens=prompt_tokens,
            max_gen_len=max_gen_len,
            temperature=temperature,
            top_p=top_p,
            logprobs=logprobs,
        )
        if logprobs:
            return [
                {
                    "generation": {
                        "role": "assistant",
                        "content": self.tokenizer.decode(t),
                    },
                    "tokens": [self.tokenizer.decode([x]) for x in t],
                    "logprobs": logprobs_i,
                }
                for t, logprobs_i in zip(generation_tokens, generation_logprobs)
            ]
        return [
            {
                "generation": {
                    "role": "assistant",
                    "content": self.tokenizer.decode(t),
                },
            }
            for t in generation_tokens
        ]

def build_args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "-f",
        "--pte",
        type=str,
        default=None,
        help="pte model file",
    )

    parser.add_argument(
        "-p",
        "--params",
        type=str,
        default=None,
        help="model params file",
    )

    parser.add_argument(
        "-t",
        "--tokenizer",
        type=str,
        default=None,
        help="tokenizer file",
    )

    parser.add_argument(
        "--prompt",
        type=str,
        default="Hello",
    )

    parser.add_argument(
        "--temperature",
        type=float,
        default=0.6,
    )

    parser.add_argument(
        "-kv",
        "--use_kv_cache",
        default=False,
        action="store_true",
    )

    return parser

def main() -> None:
    parser = build_args_parser()
    args = parser.parse_args()
    model_args: ModelArgs = ModelArgs(
        max_seq_len=128,
        max_batch_size=1,
        vocab_size=128256,
        dim=4096,
        multiple_of=1024,
        n_heads=32,
        n_layers=32.,
    )
    runner = LlamaRunner(args.pte, model_args, args.tokenizer)
    res = runner.text_completion(prompts=[args.prompt], max_gen_len=10, temperature=args.temperature, use_kv_cache=args.use_kv_cache)
    print(f"result: {res}")

if __name__ == "__main__":
    main()  # pragma: no cover