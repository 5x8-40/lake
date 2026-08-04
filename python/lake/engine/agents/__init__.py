"""可替换 StorageAgent 实现：memory（单测）/ grpc_skeleton（P3/P4 TcpData）。"""

from lake.engine.agents.memory import InMemoryAgent

__all__ = ["InMemoryAgent", "GrpcSkeletonAgent"]


def __getattr__(name: str):
    if name == "GrpcSkeletonAgent":
        from lake.engine.agents.grpc_skeleton import GrpcSkeletonAgent

        return GrpcSkeletonAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
