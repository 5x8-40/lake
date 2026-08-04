"""已废止为实现树——请用 `engine` + `runtime`（角色=prefill）。

保留空包仅为过渡期 import 兼容；C3 后业务代码勿再新增于此。
"""

from lake.pb import lake_pb2, schema_pb2

_ = schema_pb2.KVBlockID
_ = lake_pb2.RegisterBlocksRequest
