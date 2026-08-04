"""启动 P3 mock WorkerService。

  python -m lake.runtime
环境变量:
  LAKE_WORKER_BIND   默认 [::]:50053
  LAKE_CP_ADDR       默认 127.0.0.1:50051
  LAKE_KV_ADDR       默认 127.0.0.1:50052
"""

from __future__ import annotations

import logging
import os

from lake.runtime.worker import serve


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    bind = os.environ.get("LAKE_WORKER_BIND", "[::]:50053")
    cp = os.environ.get("LAKE_CP_ADDR", "127.0.0.1:50051")
    kv = os.environ.get("LAKE_KV_ADDR", "127.0.0.1:50052")
    serve(bind, cp, kv)


if __name__ == "__main__":
    main()
