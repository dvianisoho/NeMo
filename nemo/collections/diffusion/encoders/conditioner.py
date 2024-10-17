# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

import torch
import torch.nn as nn
from typing import Union
from transformers import CLIPTextModel, CLIPTokenizer, T5EncoderModel, T5Tokenizer


class AbstractEmbModel(nn.Module):
    def __init__(self, enable_lora_finetune=False, target_block=[], target_module=[]):
        super().__init__()
        self._is_trainable = None
        self._ucg_rate = None
        self._input_key = None

        self.TARGET_BLOCK = target_block
        self.TARGET_MODULE = target_module
        if enable_lora_finetune:
            self.lora_layers = []

    @property
    def is_trainable(self) -> bool:
        return self._is_trainable

    @property
    def ucg_rate(self) -> Union[float, torch.Tensor]:
        return self._ucg_rate

    @property
    def input_key(self) -> str:
        return self._input_key

    @is_trainable.setter
    def is_trainable(self, value: bool):
        self._is_trainable = value

    @ucg_rate.setter
    def ucg_rate(self, value: Union[float, torch.Tensor]):
        self._ucg_rate = value

    @input_key.setter
    def input_key(self, value: str):
        self._input_key = value

    @is_trainable.deleter
    def is_trainable(self):
        del self._is_trainable

    @ucg_rate.deleter
    def ucg_rate(self):
        del self._ucg_rate

    @input_key.deleter
    def input_key(self):
        del self._input_key

    def encode(self, *args, **kwargs):
        raise NotImplementedError

    def _enable_lora(self, lora_model):
        for module_name, module in lora_model.named_modules():
            if module.__class__.__name__ in self.TARGET_BLOCK:
                tmp = {}
                for sub_name, sub_module in module.named_modules():
                    if sub_module.__class__.__name__ in self.TARGET_MODULE:
                        if hasattr(sub_module, "input_size") and hasattr(
                            sub_module, "output_size"
                        ):  # for megatron ParallelLinear
                            lora = LoraWrapper(sub_module, sub_module.input_size, sub_module.output_size)
                        else:  # for nn.Linear
                            lora = LoraWrapper(sub_module, sub_module.in_features, sub_module.out_features)
                        self.lora_layers.append(lora)
                        if sub_name not in tmp.keys():
                            tmp.update({sub_name: lora})
                        else:
                            print(f"Duplicate subnames are found in module {module_name}")
                for sub_name, lora_layer in tmp.items():
                    lora_name = f'{sub_name}_lora'
                    module.add_module(lora_name, lora_layer)


class FrozenCLIPEmbedder(AbstractEmbModel):
    """Uses the CLIP transformer encoder for text (from Hugging Face)"""

    LAYERS = ["last", "pooled", "hidden"]

    def __init__(
        self,
        version="openai/clip-vit-large-patch14",
        device="cuda",
        max_length=77,
        enable_lora_finetune=False,
        layer="last",
        layer_idx=None,
        always_return_pooled=False,
        dtype=torch.float,
    ):
        super().__init__(enable_lora_finetune, target_block=["CLIPAttention", "CLIPMLP"], target_module=["Linear"])
        self.tokenizer = CLIPTokenizer.from_pretrained("openai/clip-vit-large-patch14")
        self.transformer = CLIPTextModel.from_pretrained(version, torch_dtype=dtype).to(device)
        self.device = device
        self.max_length = max_length
        self.freeze()
        if enable_lora_finetune:
            self._enable_lora(self.transformer)
            print(f"CLIP transformer encoder add {len(self.lora_layers)} lora layers.")

        self.layer = layer
        self.layer_idx = layer_idx
        self.return_pooled = always_return_pooled
        if layer == "hidden":
            assert layer_idx is not None
            assert 0 <= abs(layer_idx) <= 12

    def freeze(self):
        self.transformer = self.transformer.eval()
        for param in self.parameters():
            param.requires_grad = False

    def forward(self, text, max_sequence_length=None):
        batch_encoding = self.tokenizer(
            text,
            truncation=True,
            max_length=max_sequence_length if max_sequence_length else self.max_length,
            return_length=True,
            return_overflowing_tokens=False,
            padding="max_length",
            return_tensors="pt",
        )
        tokens = batch_encoding["input_ids"].to(self.transformer.device, non_blocking=True)
        outputs = self.transformer(input_ids=tokens, output_hidden_states=(self.layer == "hidden"))

        if self.layer == "last":
            z = outputs.last_hidden_state
        elif self.layer == "pooled":
            z = outputs.pooler_output[:, None, :]
        else:
            z = outputs.hidden_states[self.layer_idx]

        # Pad the seq length to multiple of 8
        seq_len = (z.shape[1] + 8 - 1) // 8 * 8
        z = torch.nn.functional.pad(z, (0, 0, 0, seq_len - z.shape[1]), value=0.0)
        if self.return_pooled:
            return z, outputs.pooler_output
        return z

    def encode(self, text):
        return self(text)


class FrozenT5Embedder(AbstractEmbModel):
    def __init__(
        self,
        version="google/t5-v1_1-xxl",
        max_length=512,
        device="cuda",
        dtype=torch.float,
    ):
        super().__init__()
        self.tokenizer = T5Tokenizer.from_pretrained("google/t5-v1_1-xxl", max_length=max_length)
        self.transformer = T5EncoderModel.from_pretrained(version, torch_dtype=dtype).to(device)
        self.max_length = max_length
        self.freeze()
        self.device = device
        self.dtype = dtype

    def freeze(self):
        self.transformer = self.transformer.eval()
        for param in self.parameters():
            param.requires_grad = False

    def forward(self, text, max_sequence_length=None):
        batch_encoding = self.tokenizer(
            text,
            truncation=True,
            max_length=max_sequence_length if max_sequence_length else self.max_length,
            return_length=False,
            return_overflowing_tokens=False,
            padding="max_length",
            return_tensors="pt",
        )

        tokens = batch_encoding["input_ids"].to(self.transformer.device, non_blocking=True)
        outputs = self.transformer(input_ids=tokens, output_hidden_states=None)

        return outputs.last_hidden_state