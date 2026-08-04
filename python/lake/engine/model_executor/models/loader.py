"""Model loader skeletons.

对齐 vLLM: loader 统一创建模型，具体 load format 只实现权重加载差异。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Generic, Literal, TypeVar


TModel = TypeVar("TModel")
TConfig = TypeVar("TConfig")
LoadFormat = Literal["dummy", "hf"]


class BaseModelLoader(Generic[TModel, TConfig]):
    """Base loader: create model first, then delegate weight loading."""

    def load_model(self, model_cls: type[TModel], config: TConfig) -> TModel:
        model = model_cls(config)
        loaded = self.load_weights(model)
        if loaded is not None:
            setattr(model, "loaded_weights", loaded)
        return model

    def load_weights(self, model: TModel) -> set[str] | None:
        raise NotImplementedError


class DummyModelLoader(BaseModelLoader[TModel, TConfig]):
    """Loader that initializes fake weights through the model's load_weights API."""

    def __init__(self, weight_names: Iterable[str] | None = None) -> None:
        self._weight_names = tuple(weight_names) if weight_names is not None else None

    def load_weights(self, model: TModel) -> set[str]:
        load_weights = getattr(model, "load_weights")
        loaded = load_weights(self.iter_dummy_weights(model))
        setattr(model, "loaded_dummy_weights", True)
        return loaded

    def iter_dummy_weights(self, model: TModel) -> Iterable[tuple[str, object]]:
        names = self._weight_names
        if names is None:
            state_dict = getattr(model, "state_dict")
            names = tuple(state_dict().keys())
        for name in names:
            yield name, object()


class DefaultModelLoader(BaseModelLoader[TModel, TConfig]):
    """Loader for real weight files.

    The file iterator is intentionally not implemented yet; this class fixes the
    boundary that future safetensors/bin loading will fill in.
    """

    def __init__(self, model_path: str, revision: str = "") -> None:
        self.model_path = model_path
        self.revision = revision

    def load_weights(self, model: TModel) -> set[str] | None:
        raise NotImplementedError("real weight loading is not implemented yet")


def get_model_loader(
    load_format: LoadFormat,
    *,
    model_path: str = "",
    revision: str = "",
) -> BaseModelLoader[TModel, TConfig]:
    if load_format == "dummy":
        return DummyModelLoader()
    if load_format == "hf":
        return DefaultModelLoader(model_path=model_path, revision=revision)
    raise ValueError(f"unsupported load_format={load_format!r}")
