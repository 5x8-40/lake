"""Model registry for compute runners.

Mirrors vLLM's split: HF config loading happens at model load time, and the
registry only resolves config.architectures to a lazily imported model class.
The runner should not know concrete model classes.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from pathlib import Path
from typing import Any

from transformers import AutoConfig


class _BaseRegisteredModel:
    def load_model_cls(self) -> type:
        raise NotImplementedError


@dataclass(frozen=True)
class _RegisteredModel(_BaseRegisteredModel):
    model_cls: type

    def load_model_cls(self) -> type:
        return self.model_cls


@dataclass(frozen=True)
class _LazyRegisteredModel(_BaseRegisteredModel):
    module_name: str
    class_name: str

    def load_model_cls(self) -> type:
        mod = importlib.import_module(self.module_name)
        return getattr(mod, self.class_name)


@dataclass(frozen=True)
class LoadedModel:
    model_path: str
    revision: str
    backend: str
    load_format: str = "dummy"
    config: Any | None = None
    model: object | None = None
    load_dummy_weights: bool = False


@dataclass
class _ModelRegistry:
    models: dict[str, _BaseRegisteredModel]

    def get_supported_archs(self) -> tuple[str, ...]:
        return tuple(sorted(self.models))

    def register_model(self, model_arch: str, model_cls: type | str) -> None:
        if not isinstance(model_arch, str) or not model_arch:
            raise TypeError("model_arch must be a non-empty string")
        if isinstance(model_cls, str):
            module_name, sep, class_name = model_cls.partition(":")
            if not sep or not module_name or not class_name:
                raise ValueError("Expected model_cls string in '<module>:<class>' format")
            model = _LazyRegisteredModel(module_name, class_name)
        elif isinstance(model_cls, type):
            model = _RegisteredModel(model_cls)
        else:
            raise TypeError("model_cls must be a class or '<module>:<class>' string")
        self.models[model_arch] = model

    def resolve_model_cls(self, architectures: list[str]) -> tuple[type, str]:
        for arch in architectures:
            model = self.models.get(arch)
            if model is not None:
                return model.load_model_cls(), arch
        self._raise_for_unsupported(architectures)

    def _raise_for_unsupported(self, architectures: list[str]) -> None:
        raise NotImplementedError(
            f"Model architectures {architectures} are not supported for now. "
            f"Supported architectures: {self.get_supported_archs()}"
        )


ModelRegistry = _ModelRegistry(models={})
ModelRegistry.register_model(
    "Qwen3ForCausalLM",
    "lake.engine.model_executor.models.qwen.qwen3:Qwen3ForCausalLM",
)

def load_hf_config(model_path: str, revision: str = "") -> Any:
    kwargs: dict[str, object] = {}
    if revision and not Path(model_path).expanduser().exists():
        kwargs["revision"] = revision
    return AutoConfig.from_pretrained(model_path, **kwargs)


def load_registered_model(
    *,
    backend: str,
    model_path: str,
    revision: str = "",
    load_format: str = "dummy",
    config_override: Any | None = None,
) -> LoadedModel:
    config = config_override or load_hf_config(model_path, revision)
    architectures = list(getattr(config, "architectures", None) or [])
    model_cls, _ = ModelRegistry.resolve_model_cls(architectures)

    from lake.engine.model_executor.models.loader import get_model_loader

    loader = get_model_loader(load_format, model_path=model_path, revision=revision)
    model = loader.load_model(model_cls, config)
    return LoadedModel(
        model_path=model_path,
        revision=revision,
        backend=backend,
        load_format=load_format,
        config=config,
        model=model,
        load_dummy_weights=load_format == "dummy",
    )
