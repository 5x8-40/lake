# Ascend bring-up scripts

单机 / 跨机 discovery 启动脚本。说明见 [`../../native-bringup.md`](../../native-bringup.md)。

```bash
export WM_ROOT=/data/wm   # dynamo-ascend 检出目录、日志、etcd 数据默认根

bash scripts/ascend/start_docker_va.sh
bash scripts/ascend/start_etcd.sh
bash scripts/ascend/start_dynamo_va_native.sh

RESTART=1 bash scripts/ascend/start_dynamo_va_native.sh
DISCOVERY=file bash scripts/ascend/start_dynamo_va_native.sh
ETCD_ENDPOINTS=http://<host-ip>:2379 bash scripts/ascend/start_dynamo_va_native.sh
bash scripts/ascend/start_dynamo_va_native.sh stop
```
