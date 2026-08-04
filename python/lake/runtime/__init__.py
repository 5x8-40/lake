"""计算层 runtime:WorkerEngine + node_scheduler（Host Req 权威）+ WorkerService。

gRPC stub 校验延后到 `worker` / `serve` 路径，避免无 grpc 环境下单测无法 import。
"""
