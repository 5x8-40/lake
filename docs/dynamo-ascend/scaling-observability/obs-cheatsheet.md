# 可观测性配置速查

> 专题索引:[README.md](README.md) · 代码级剖析,基于 `3rdparty/dynamo`

下面是一张"抄作业"表格 — 列出可观测性相关的所有关键环境变量及其默认值。大部分可观测功能默认关闭，需要手动开启。

### System Server（/metrics、/health、/live）

| 环境变量 | 默认值 | 说明 | 大白话 |
| --- | --- | --- | --- |
| `DYN_SYSTEM_PORT` | `-1`（关闭） | 设为 >=0 开启，0 为随机端口 | "指标服务器端口，默认关着" |
| `DYN_SYSTEM_HOST` | `0.0.0.0` | 绑定地址 | "监听哪个 IP" |
| `DYN_SYSTEM_HEALTH_PATH` | `/health` | 健康检查路径 | — |
| `DYN_SYSTEM_LIVE_PATH` | `/live` | 存活检查路径 | — |
| `DYN_SYSTEM_STARTING_HEALTH_STATUS` | `NotReady` | 初始状态：Ready / NotReady | "启动时说不说能干活" |

### Health Check（金丝雀探测）

| 环境变量 | 默认值 | 说明 | 大白话 |
| --- | --- | --- | --- |
| `DYN_HEALTH_CHECK_ENABLED` | `false` | 启用主动健康检查 | "要不要主动试探后端，默认关着" |
| `DYN_CANARY_WAIT_TIME` | 10s | 空闲等待时间 | "多久没请求就试探一次" |
| `DYN_HEALTH_CHECK_REQUEST_TIMEOUT` | 3s | 单次探测超时 | "试探包等多久算超时" |

### 日志（Log）

| 环境变量 | 默认值 | 说明 | 大白话 |
| --- | --- | --- | --- |
| `DYN_LOG` | `info` | 日志级别，支持 `target=level` 语法 | "日志详细程度" |
| `DYN_LOGGING_CONSOLE_FORMAT` | `readable` | 设为 `jsonl` 输出结构化 JSON 日志 | "日志格式：人看还是机器看" |
| `DYN_LOG_USE_LOCAL_TZ` | false（UTC） | 使用本地时区 | "日志时间戳用哪个时区" |

### OTLP 导出（Traces + Logs + Metrics）

| 环境变量 | 默认值 | 说明 | 大白话 |
| --- | --- | --- | --- |
| `OTEL_EXPORT_ENABLED` | 关闭 | 设为 `1` 启用 traces+logs 导出 | "追踪和日志导出，默认关着" |
| `OTEL_METRICS_EXPORTER` | 关闭 | 设为 `otlp` 启用 metrics 导出 | "指标 OTLP 推送，默认关着" |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP gRPC 端点 | "数据发到哪" |
| `OTEL_SERVICE_NAME` | `dynamo` | 服务名称 | "这个组件叫什么" |
| `OTEL_METRIC_EXPORT_INTERVAL` | 60000ms（60s） | 指标推送间隔（毫秒） | "多久推一次指标" |
| `OTEL_TRACES_SAMPLE_RATIO` | 1.0（全采） | 采样率 0.0-1.0 | "抓多少追踪数据" |

### Request Trace（请求审计日志）

| 环境变量 | 默认值 | 说明 | 大白话 |
| --- | --- | --- | --- |
| `DYN_REQUEST_TRACE` | 关闭 | 设为 `1` 开启 | "逐请求审计，默认关着" |
| `DYN_REQUEST_TRACE_SINKS` | `file` | `file,stderr,nats,otel,s3` | "记录存到哪" |
| `DYN_REQUEST_TRACE_RECORDS` | `request_end,tool` | 记录类型 | "记哪些事件" |
| `DYN_REQUEST_TRACE_FILE_PATH` | `/tmp/dynamo-request-trace` | 文件输出路径 | "文件存哪" |
| `DYN_REQUEST_TRACE_FILE_FORMAT` | `jsonl_gz` | `jsonl` / `jsonl_gz` | "纯文本还是压缩" |
| `DYN_REQUEST_TRACE_CAPACITY` | 1024 | 内存队列容量，满了会丢数据 | "排队 buffer 多大" |

### Frontend 指标直方图桶

Frontend 的直方图指标（TTFT、ITL、请求时长等）支持自定义桶边界。通过 `DYN_METRICS_TTFT_MIN`、`DYN_METRICS_TTFT_MAX`、`DYN_METRICS_TTFT_COUNT` 等环境变量控制。类似地还有 `DYN_METRICS_ITL_*`、`DYN_METRICS_REQUEST_DURATION_*`、`DYN_METRICS_INPUT_SEQUENCE_*`、`DYN_METRICS_OUTPUT_SEQUENCE_*`。

### 一键开启所有可观测性（推荐开发环境）

```
# 开启 /metrics + /health + /live
export DYN_SYSTEM_PORT=8081

# 开启 OTel Traces + Logs 导出
export OTEL_EXPORT_ENABLED=1
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
export OTEL_SERVICE_NAME=dynamo-backend

# 开启 OTel Metrics 推送（可选，Prometheus Pull 已够用的话不需要）
export OTEL_METRICS_EXPORTER=otlp

# 开启结构化 JSON 日志（可选，方便 Loki 采集）
export DYN_LOGGING_CONSOLE_FORMAT=jsonl

# 开启金丝雀健康检查（可选）
export DYN_HEALTH_CHECK_ENABLED=1

# 启动 Dynamo worker
DYN_SYSTEM_PORT=8081 python -m dynamo.vllm --model $MODEL
```

