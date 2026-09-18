# Dynamo-Ascend bring-up scripts

Ascend 单机 / 跨机 discovery 启动脚本。说明见  
[`docs/dynamo-ascend/2026-09-18-e14-e15-native-bringup.md`](../../docs/dynamo-ascend/2026-09-18-e14-e15-native-bringup.md)。

```bash
export WM_ROOT=/data/wm   # dynamo-ascend 源码、日志、etcd 数据默认根目录

bash scripts/dynamo-ascend/start_docker_va.sh      # 常驻 vllm-ascend 容器
bash scripts/dynamo-ascend/start_etcd.sh            # 常驻 etcd :2379
bash scripts/dynamo-ascend/start_dynamo_va_native.sh  # 容器内 FE + worker

# 可选
RESTART=1 bash scripts/dynamo-ascend/start_dynamo_va_native.sh
DISCOVERY=file bash scripts/dynamo-ascend/start_dynamo_va_native.sh
ETCD_ENDPOINTS=http://<host-ip>:2379 bash scripts/dynamo-ascend/start_dynamo_va_native.sh
bash scripts/dynamo-ascend/start_dynamo_va_native.sh stop
```
