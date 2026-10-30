"""E14 发布端：READY 后按目标实例补发一条 BlockStored。

注入假的 Zmq 发布器，不绑端口，也不 import vllm。
"""

from __future__ import annotations

import logging

import pytest

from conftest import run
from kvcm.engines.vllm.keys import TokenBlockChain, hash_token_block_chain
from kvcm.events.publisher import BlockStoredPublisher, parse_kv_event_endpoints
from kvcm.flows.prefetch import PrefetchError
from kvcm.ops.base import TaskStatus
from kvcm.ops.types import InstanceId
from kvcm.task_engine import TaskEngine
from test_task_engine import FakeKeepalive, FakeOps, _H, _P, _T

# hash_block_tokens 的原值。十六进制串是进 PoolKey 的那一步，事件里不能出现。
_RAW = b"\x01\x02\xff"
_HEX = _RAW.hex()
_ENDPOINT = "tcp://*:5557"


class _Stored:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class _Batch:
    def __init__(self, ts, events, data_parallel_rank=None) -> None:
        self.ts = ts
        self.events = events
        self.data_parallel_rank = data_parallel_rank


class _Zmq:
    def __init__(self, endpoint: str, sink: list) -> None:
        self.endpoint = endpoint
        self.batches: list = []
        self.closed = False
        sink.append(self)

    def publish(self, batch) -> None:
        self.batches.append(batch)

    def shutdown(self) -> None:
        self.closed = True


def _events_fn():
    """替掉上游 BlockStored / EventBatch / MEDIUM_CPU。MEDIUM_CPU 就是 "CPU"。"""
    return _Stored, _Batch, "CPU", None


def _chain(token_ids, block_size, hash_algo):
    n_full = (len(token_ids) // block_size) * block_size
    return TokenBlockChain(
        block_hashes=(_RAW,),
        parent_block_hash=None,
        token_ids=token_ids[:n_full],
        block_size=block_size,
    )


def _publisher(endpoints, sink, *, block_size=2):
    def factory(endpoint: str) -> _Zmq:
        return _Zmq(endpoint, sink)

    return BlockStoredPublisher(
        endpoints,
        block_size=block_size,
        hash_algo="sha256",
        chain_fn=_chain,
        publisher_factory=factory,
        events_fn=_events_fn,
    )


def test_default_chain_is_raw_hash() -> None:
    """默认走原值链，不把十六进制 PoolKey 当成事件 hash。"""
    pub = BlockStoredPublisher({}, block_size=128, hash_algo="sha256")
    assert pub._chain_fn is hash_token_block_chain


def test_ready_publishes_one_with_raw_hash_and_cpu(monkeypatch) -> None:
    """READY 后发一条：hash 是原值，medium 是 CPU，端点原样、rank 固定 0。"""
    import builtins

    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "vllm" or name.startswith("vllm."):
            raise AssertionError(f"测试路径 import 了 {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    sink: list[_Zmq] = []
    pub = _publisher({str(_T): _ENDPOINT}, sink)
    ka = FakeKeepalive()
    eng = TaskEngine(FakeOps(seq=[TaskStatus.READY]), keepalive=ka, events=pub)
    run(eng.run_once(_P, _T, poll_s=0.01))
    assert ka.registered == [_P]
    assert len(sink) == 1
    assert sink[0].endpoint == _ENDPOINT  # 不做 DP 端口偏移
    assert len(sink[0].batches) == 1
    batch = sink[0].batches[0]
    event = batch.events[0]
    assert event.block_hashes == [_RAW]
    assert _HEX not in event.block_hashes
    assert all(not isinstance(h, str) for h in event.block_hashes)
    assert event.medium == "CPU"
    assert event.parent_block_hash is None
    assert event.token_ids == [1, 2]  # 满块；尾巴 token 3 不进事件
    assert event.block_size == 2
    assert batch.data_parallel_rank == 0


def test_same_instance_constructs_endpoint_once() -> None:
    """同一实例第二次补发复用已构造的发布器；另一个实例才再构造。"""
    sink: list[_Zmq] = []
    other = InstanceId("inst-A")
    pub = _publisher({str(_T): _ENDPOINT, str(other): "tcp://*:5558"}, sink)
    pub.publish_ready(_P, _T)
    pub.publish_ready(_P, _T)
    pub.publish_ready(_P, other)
    assert [z.endpoint for z in sink] == [_ENDPOINT, "tcp://*:5558"]
    assert len(sink[0].batches) == 2
    assert len(sink[1].batches) == 1


def test_missing_endpoint_prefetch_still_succeeds(caplog) -> None:
    """没有该实例的端点：记一次日志，预热成功，保活仍注册，不构造发布器。"""
    sink: list[_Zmq] = []
    pub = _publisher({}, sink)
    ka = FakeKeepalive()
    eng = TaskEngine(FakeOps(seq=[TaskStatus.READY]), keepalive=ka, events=pub)
    with caplog.at_level(logging.WARNING, logger="kvcm.events.publisher"):
        run(eng.run_once(_P, _T, poll_s=0.01))
        run(eng.run_once(_P, _T, poll_s=0.01))
    assert ka.registered == [_P, _P]
    assert sink == []
    missing = [r for r in caplog.records if "inst-B" in r.getMessage()]
    assert len(missing) == 1


def test_failure_does_not_publish() -> None:
    sink: list[_Zmq] = []
    pub = _publisher({str(_T): _ENDPOINT}, sink)
    ka = FakeKeepalive()
    eng = TaskEngine(FakeOps(seq=[TaskStatus.FAILED]), keepalive=ka, events=pub)
    with pytest.raises(PrefetchError):
        run(eng.run_once(_P, _T, poll_s=0.01))
    assert ka.registered == []
    assert sink == []


def test_publish_error_does_not_fail_ready() -> None:
    """补发抛错也不能把已核验的预热打成失败。"""

    class Boom:
        def publish_ready(self, prefix, target) -> None:
            raise RuntimeError("zmq down")

        def shutdown(self) -> None:
            return None

    ka = FakeKeepalive()
    eng = TaskEngine(FakeOps(seq=[TaskStatus.READY]), keepalive=ka, events=Boom())
    run(eng.run_once(_P, _T, poll_s=0.01))
    assert ka.registered == [_P]


def test_track_ready_publishes_when_target_known() -> None:
    """恢复路径：轮询前能拿到目标实例时，READY 同样补发一条。"""

    class Ops(FakeOps):
        def target_of(self, handle):
            return _T

    sink: list[_Zmq] = []
    pub = _publisher({str(_T): _ENDPOINT}, sink)
    ka = FakeKeepalive()
    eng = TaskEngine(Ops(seq=[TaskStatus.READY]), keepalive=ka, events=pub)
    run(eng.track(_H, poll_s=0.01))
    assert ka.registered == [_P]
    assert len(sink) == 1
    assert len(sink[0].batches) == 1
    assert sink[0].batches[0].events[0].block_hashes == [_RAW]


def test_track_without_target_still_registers_keepalive() -> None:
    """target_of 默认 None：恢复路径不补发，保活仍注册，预热不算失败。"""
    sink: list[_Zmq] = []
    pub = _publisher({str(_T): _ENDPOINT}, sink)
    ka = FakeKeepalive()
    eng = TaskEngine(FakeOps(seq=[TaskStatus.READY]), keepalive=ka, events=pub)
    run(eng.track(_H, poll_s=0.01))
    assert ka.registered == [_P]
    assert sink == []


def test_parse_kv_event_endpoint() -> None:
    got = parse_kv_event_endpoints(
        ["inst-B=tcp://*:5557", "inst-A=tcp://127.0.0.1:9"]
    )
    assert got == {"inst-B": "tcp://*:5557", "inst-A": "tcp://127.0.0.1:9"}
    assert parse_kv_event_endpoints(None) == {}
    with pytest.raises(ValueError):
        parse_kv_event_endpoints(["no-equals"])
    with pytest.raises(ValueError):
        parse_kv_event_endpoints(["a=tcp://*:1", "a=tcp://*:2"])


def test_engine_shutdown_closes_publisher() -> None:
    sink: list[_Zmq] = []
    pub = _publisher({str(_T): _ENDPOINT}, sink)

    async def scenario():
        eng = TaskEngine(FakeOps(), events=pub)
        eng.start()
        pub.publish_ready(_P, _T)
        await eng.shutdown(timeout_s=1)

    run(scenario())
    assert len(sink) == 1
    assert sink[0].closed
