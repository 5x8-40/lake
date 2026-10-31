"""空写软钉：拿官方包已经建好的 client，调 UpsertStart / UpsertEnd。"""

from __future__ import annotations


def empty_soft_pin(capsule: object, keys: list[str], sizes: list[int], enable: bool) -> None:
    """capsule 来自 store._get_pyclient_capsule()，里面是已有的 PyClient。"""
    from kvcm.native import _empty_pin

    _empty_pin.empty_soft_pin(capsule, list(keys), list(sizes), bool(enable))
