"""vLLM / vllm-ascend 引擎适配。

key 计算（KeyComputer）与 rank 拓扑（RankTopology）是引擎强关联物：
key 的权威定义在引擎（引擎写 KV 即以此 key 落池，存储池对 key 无格式
要求）；rank 与 KV shard 的切分形状随引擎与模型配置变。
"""

from kvcm.engines.vllm.keys import KeyComputer, group_keys_by_rank, parse_key_rank

__all__ = ["KeyComputer", "group_keys_by_rank", "parse_key_rank"]
