"""空写软钉：拿官方包已经建好的 client，调 UpsertStart / UpsertEnd。

加载时硬闸（R04 护栏，见 D2 §2 ABI）：
1. 已装 Mooncake 发行版版本须 = ``EXPECTED_MOONCAKE_VERSION``；
2. ``_empty_pin.so`` 镜像类型的 sizeof / offsetof 须与 A14 记录一致。

挡的是「升 wheel 却仍用旧 so / 改坏头文件仍去调 Upsert」。
挡不住「同版本号、不同构建」的 SHA 级漂移——那仍要重编 + 行为验。
"""

from __future__ import annotations

from importlib import metadata
from typing import Any

# 与 D2 版本基线、mooncake_pin_api.h 注释一致；升基线时三处一起改。
EXPECTED_MOONCAKE_VERSION = "0.3.13.post1"

# A14 同机探针（镜像头按 v0.3.13.post1）。改头文件布局必须同步改这里。
EXPECTED_ABI_LAYOUT: dict[str, int] = {
    "ObjectMeta": 48,
    "ReplicateConfig": 208,
    "EndResult": 8,
    "PyClient.client_": 8,
    "Client.master_client_": 48,
}

_MOONCAKE_DISTS = (
    "mooncake-transfer-engine-npu",
    "mooncake-transfer-engine",
)


def mooncake_installed_version() -> str | None:
    """读已装 Mooncake wheel 的版本；找不到发行版返回 None。"""
    for name in _MOONCAKE_DISTS:
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return None


def ensure_empty_pin_abi(*, ext: Any | None = None) -> Any:
    """版本 + 布局闸。通过则返回已加载的 ``_empty_pin`` 模块。"""
    installed = mooncake_installed_version()
    if installed is None:
        raise RuntimeError(
            "未找到 Mooncake 发行版"
            f"（试过 {', '.join(_MOONCAKE_DISTS)}），没有钉上"
        )
    if installed != EXPECTED_MOONCAKE_VERSION:
        raise RuntimeError(
            f"Mooncake 版本 {installed} ≠ 空写承诺 {EXPECTED_MOONCAKE_VERSION}；"
            "升 wheel 后须重编 _empty_pin.so 并同步改 EXPECTED_MOONCAKE_VERSION，"
            "没有钉上"
        )
    if ext is None:
        from kvcm.native import _empty_pin as ext  # type: ignore[attr-defined]
    probe = getattr(ext, "abi_layout", None)
    if not callable(probe):
        raise RuntimeError(
            "_empty_pin.so 缺少 abi_layout；请用当前源码重编，没有钉上"
        )
    layout = dict(probe())
    for key, want in EXPECTED_ABI_LAYOUT.items():
        got = layout.get(key)
        if got != want:
            raise RuntimeError(
                f"_empty_pin ABI 探针 {key}={got} ≠ 期望 {want}；"
                "头文件/so 与 A14 基线不一致，请重编，没有钉上"
            )
    return ext


def empty_soft_pin(
    capsule: object,
    keys: list[str],
    sizes: list[int],
    addresses: list[int],
    enable: bool,
) -> None:
    """capsule 来自 store._get_pyclient_capsule()，里面是已有的 PyClient。"""
    ext = ensure_empty_pin_abi()
    ext.empty_soft_pin(
        capsule, list(keys), list(sizes), list(addresses), bool(enable)
    )
