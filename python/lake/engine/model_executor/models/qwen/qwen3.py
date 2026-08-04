"""Qwen3 model runtime skeleton.

类名与 vLLM `Qwen3ForCausalLM` 对齐；config 由模型加载侧从 Hugging Face
config 读取后传入。本阶段经通用 `DummyModelLoader` 建立权重加载边界和
deterministic forward 占位，不加载真实 safetensors。
"""

from __future__ import annotations

from collections.abc import Iterable

import torch
from torch import nn
from transformers import Qwen3Config

from lake.engine.model_executor.layers.attentions import TorchAttentionBackend


def _param_dtype(config: Qwen3Config) -> torch.dtype:
    dtype = getattr(config, "dtype", None) or getattr(config, "torch_dtype", None)
    if isinstance(dtype, torch.dtype):
        return dtype
    if isinstance(dtype, str) and hasattr(torch, dtype):
        value = getattr(torch, dtype)
        if isinstance(value, torch.dtype):
            return value
    return torch.bfloat16


def _head_dim(config: Qwen3Config) -> int:
    return int(getattr(config, "head_dim", config.hidden_size // config.num_attention_heads))


class Qwen3RMSNorm(nn.Module):
    """RMSNorm parameter shell matching Qwen3/vLLM naming."""

    def __init__(self, hidden_size: int, eps: float = 1e-6, dtype: torch.dtype = torch.bfloat16) -> None:
        super().__init__()
        self.weight = nn.Parameter(torch.empty(hidden_size, device="meta", dtype=dtype))
        self.variance_epsilon = eps

    def forward(self, hidden_states: object, residual: object | None = None) -> object:
        return hidden_states if residual is None else (hidden_states, residual)


class Qwen3RotaryEmbedding(nn.Module):
    """RoPE metadata placeholder; real kernel wiring belongs to a later phase."""

    def __init__(self, config: Qwen3Config) -> None:
        super().__init__()
        self.head_dim = _head_dim(config)
        self.max_position_embeddings = config.max_position_embeddings
        self.rope_parameters = getattr(config, "rope_parameters", None)

    def forward(self, positions: object, q: object, k: object) -> tuple[object, object]:
        return q, k


class Qwen3PagedAttention(nn.Module):
    """Paged-attention placeholder with the same ownership boundary as vLLM."""

    def __init__(self, num_heads: int, head_dim: int, num_kv_heads: int) -> None:
        super().__init__()
        self.num_heads = num_heads
        self.head_dim = head_dim
        self.num_kv_heads = num_kv_heads
        self.backend = TorchAttentionBackend()

    def forward(self, q: object, k: object, v: object) -> object:
        if (
            isinstance(q, torch.Tensor)
            and isinstance(k, torch.Tensor)
            and isinstance(v, torch.Tensor)
        ):
            return self.backend.forward_tensors(
                q,
                k,
                v,
                is_causal=True,
                scale=self.head_dim**-0.5,
            )
        return q


class Qwen3Attention(nn.Module):
    """Qwen3 attention shell following vLLM's packed qkv projection layout."""

    def __init__(self, config: Qwen3Config, layer_idx: int) -> None:
        super().__init__()
        dtype = _param_dtype(config)
        self.config = config
        self.layer_idx = layer_idx
        self.hidden_size = config.hidden_size
        self.total_num_heads = config.num_attention_heads
        self.total_num_kv_heads = config.num_key_value_heads
        self.head_dim = _head_dim(config)
        self.q_size = self.total_num_heads * self.head_dim
        self.kv_size = self.total_num_kv_heads * self.head_dim
        self.scaling = self.head_dim**-0.5
        self.qkv_proj = nn.Linear(
            self.hidden_size,
            self.q_size + (2 * self.kv_size),
            bias=getattr(config, "attention_bias", False),
            device="meta",
            dtype=dtype,
        )
        self.o_proj = nn.Linear(
            self.q_size,
            self.hidden_size,
            bias=False,
            device="meta",
            dtype=dtype,
        )
        self.rotary_emb = Qwen3RotaryEmbedding(config)
        self.attn = Qwen3PagedAttention(
            num_heads=self.total_num_heads,
            head_dim=self.head_dim,
            num_kv_heads=self.total_num_kv_heads,
        )
        self.q_norm = Qwen3RMSNorm(self.head_dim, eps=config.rms_norm_eps, dtype=dtype)
        self.k_norm = Qwen3RMSNorm(self.head_dim, eps=config.rms_norm_eps, dtype=dtype)

    def forward(self, positions: object, hidden_states: object) -> object:
        return hidden_states


class Qwen3MLP(nn.Module):
    """Qwen3 MLP shell using vLLM's packed gate_up projection naming."""

    def __init__(self, config: Qwen3Config) -> None:
        super().__init__()
        dtype = _param_dtype(config)
        self.gate_up_proj = nn.Linear(
            config.hidden_size,
            2 * config.intermediate_size,
            bias=False,
            device="meta",
            dtype=dtype,
        )
        self.down_proj = nn.Linear(
            config.intermediate_size,
            config.hidden_size,
            bias=False,
            device="meta",
            dtype=dtype,
        )
        self.act_fn = nn.SiLU()

    def forward(self, hidden_states: object) -> object:
        return hidden_states


class Qwen3Model(nn.Module):
    """Qwen3 decoder backbone skeleton.

    vLLM 的 `Qwen3Model` 继承 `Qwen2Model` 并替换 decoder layer 类型；
    lake 先钉住 module 边界和 config，真 attention/MLP 后续接 Torch/Triton。
    """

    def __init__(self, config: Qwen3Config) -> None:
        super().__init__()
        dtype = _param_dtype(config)
        self.config = config
        self.padding_idx = config.pad_token_id
        self.vocab_size = config.vocab_size
        self.embed_tokens = nn.Embedding(
            config.vocab_size,
            config.hidden_size,
            self.padding_idx,
            device="meta",
            dtype=dtype,
        )
        self.layers = nn.ModuleList(
            Qwen3DecoderLayer(config, layer_idx=i) for i in range(config.num_hidden_layers)
        )
        self.norm = Qwen3RMSNorm(config.hidden_size, eps=config.rms_norm_eps, dtype=dtype)
        self.rotary_emb = Qwen3RotaryEmbedding(config)
        self.has_sliding_layers = "sliding_attention" in (getattr(config, "layer_types", None) or [])

    def forward(
        self,
        input_ids: object | None = None,
        positions: object | None = None,
        intermediate_tensors: object | None = None,
        inputs_embeds: object | None = None,
    ) -> object:
        if inputs_embeds is not None:
            hidden_states = inputs_embeds
        else:
            hidden_states = input_ids
        for layer in self.layers:
            hidden_states = layer(hidden_states, positions)
        return self.norm(hidden_states)


class Qwen3DecoderLayer(nn.Module):
    """Dense Qwen3 decoder layer placeholder with stable module identity."""

    def __init__(self, config: Qwen3Config, layer_idx: int) -> None:
        super().__init__()
        self.config = config
        self.layer_idx = layer_idx
        dtype = _param_dtype(config)
        self.hidden_size = config.hidden_size
        self.self_attn = Qwen3Attention(config, layer_idx)
        self.mlp = Qwen3MLP(config)
        self.input_layernorm = Qwen3RMSNorm(config.hidden_size, eps=config.rms_norm_eps, dtype=dtype)
        self.post_attention_layernorm = Qwen3RMSNorm(
            config.hidden_size,
            eps=config.rms_norm_eps,
            dtype=dtype,
        )

    def forward(self, hidden_states: object, positions: object | None = None) -> object:
        return hidden_states


class Qwen3ForCausalLM(nn.Module):
    """Qwen3 causal LM skeleton.

    对齐 vLLM `Qwen3ForCausalLM`:顶层模型继承 `nn.Module`，持有
    `self.model = Qwen3Model(...)`，并暴露 `forward` / `compute_logits` /
    `load_weights(weights)`；dummy 由 loader 层处理。
    """

    def __init__(self, config: Qwen3Config) -> None:
        super().__init__()
        self.config = config
        self.model = Qwen3Model(config)
        self.lm_head = (
            self.model.embed_tokens
            if config.tie_word_embeddings
            else nn.Linear(
                config.hidden_size,
                config.vocab_size,
                bias=False,
                device="meta",
                dtype=_param_dtype(config),
            )
        )
        self.logits_processor = nn.Identity()
        self.loaded_weights: set[str] = set()
        self.loaded_dummy_weights = False

    def forward(
        self,
        input_ids: object | None = None,
        positions: object | None = None,
        intermediate_tensors: object | None = None,
        inputs_embeds: object | None = None,
    ) -> object:
        return self.model(input_ids, positions, intermediate_tensors, inputs_embeds)

    def compute_logits(self, hidden_states: object) -> object:
        return hidden_states

    def load_weights(self, weights: Iterable[tuple[str, object]]) -> set[str]:
        self.loaded_weights = {name for name, _ in weights}
        return set(self.loaded_weights)


