# Tracing 链路追踪与 Request Trace

> 专题索引:[README.md](README.md) · 代码级剖析,基于 `3rdparty/dynamo`

## 1. Tracing 链路追踪 — 从配置到查看的完整步骤

Tracing 的大白话解释：当一个请求进入系统，前端创建一个"根 span"（就像给这个请求发了一个身份证）。然后每经过一个组件（prefill worker → decode worker），都会创建一个子 span（"我也是这个请求的一部分"）。这些 span 通过 W3C traceparent header 跨进程传播 — 就像接力赛跑，每个跑者手里拿着同一个接力棒。最后所有 span 通过 OTLP 协议推送到 Tempo 存储，你可以在 Grafana 里看到完整的请求链路树，精确到每个环节的耗时。

### 第一步：配置 Tracing 输出

#### 1A. 环境变量配置

Dynamo 的 tracing 初始化是幂等的（使用 Once 保证只执行一次）。你通过环境变量控制日志级别和 OTel 导出行为。

| 环境变量 | 默认值 | 说明 | 大白话 |
| --- | --- | --- | --- |
| `DYN_LOG` | `warn` | tracing-subscriber 的 EnvFilter，控制日志级别 | "日志详细程度" |
| `OTEL_EXPORT_ENABLED` | `0` | 设为 `1` 启用 OTel 导出 | "是否启用链路追踪" |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP gRPC 端点地址 | "追踪数据发到哪" |
| `OTEL_SERVICE_NAME` | `dynamo` | 服务名称，用于区分不同组件 | "这个组件叫什么名字" |
| `OTEL_RESOURCE_ATTRIBUTES` | 空 | 附加属性，如 `deployment.environment=staging` | "额外标签" |

```
# 启动 Dynamo 组件时设置环境变量
export DYN_LOG=info,dynamo=debug        # 总体 info，dynamo 模块 debug
export OTEL_EXPORT_ENABLED=1           # 启用 OTel 导出
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
export OTEL_SERVICE_NAME=dynamo-frontend  # 或 dynamo-backend

# 启动组件
python -m dynamo.frontend --router-mode kv
```

#### 1B. Tracing 初始化流程

init() (Once 保证幂等 — 多次调用安全)
→ setup\_logging() (logging.rs)
→ 读 `DYN_LOG` 环境变量构建 EnvFilter
→ 创建 Layered Subscriber:
├── fmt layer (stdout 输出)
├── EnvFilter (按 DYN\_LOG 过滤)
→ 若 `OTEL_EXPORT_ENABLED=1`:
├── 创建 SdkTracerProvider (opentelemetry-sdk)
├── 创建 OTLP exporter (gRPC → OTEL\_EXPORTER\_OTLP\_ENDPOINT)
├── 安装 tracing-opentelemetry layer (Rust tracing span → OTel span 桥接)
→ 安装 DistributedTraceIdLayer:
└── 每个 span 自动注入 trace\_id / span\_id 到日志行

### 第二步：跨进程 Trace 传播

W3C Trace Context 是业界标准。Dynamo 使用 `traceparent` 和 `tracestate` HTTP headers 在组件间传播 trace context。当 frontend 收到一个请求，它检查请求是否已携带 traceparent：如果有，说明上游（如 API Gateway）已经创建了 trace，Dynamo 作为子 span 加入；如果没有，Dynamo 创建一个新的根 trace。

请求到达 Frontend:
→ 检查 `traceparent` header
├── 有 → 提取 trace\_id + parent\_span\_id，创建子 span
└── 无 → 生成新 trace\_id，创建根 span "http-request"
→ Frontend 路由请求到 Worker:
├── 将当前 span 的 traceparent 写入 NATS message header
└── Worker 从 header 提取 context，创建子 span "handle\_payload"
→ Prefill → Decode (Disagg 模式):
└── 同样通过 header 传播，保持同一 trace\_id

### 第三步：启动 Tracing 后端

#### 3A. 使用 Docker 一键启动（包含 Tempo）

docker-observability.yml 已经包含了 Tempo（追踪存储）和 OTel Collector（数据中转）。一键启动即可。

```
# 启动完整可观测性栈（含 Tempo + OTel Collector）
docker compose -f dynamo/dev/observability/docker-observability.yml up -d

# 验证 Tempo 运行
curl http://localhost:3200/health

# 验证 OTel Collector 运行
curl http://localhost:13133
```

#### 3B. OTel Collector 配置

OTel Collector 是"数据中转站"。Dynamo 组件把 tracing data 发给 Collector，Collector 再转发给 Tempo 存储。你不需要每个 Dynamo 组件直连 Tempo。

```
# otel-collector-config.yaml (docker-observability.yml 中内嵌)
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 5s
    send_batch_size: 1000

exporters:
  otlp/tempo:
    endpoint: tempo:4317
    tls:
      insecure: true
  prometheus:
    endpoint: 0.0.0.0:8889

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlp/tempo]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [prometheus]
```

#### 3C. Grafana 中查看 Traces

| 步骤 | 操作 | 说明 |
| --- | --- | --- |
| ① | 打开 Grafana → Explore | — |
| ② | 数据源选择 Tempo | — |
| ③ | 点击 "Search" 标签 | 按时间/服务名筛选 traces |
| ④ | 点击任一 trace 条目 | 进入 "Trace Graph" 查看完整 span 树 |
| ⑤ | 展开 span 查看 details | 每个 span 的耗时、标签、日志 |

### 初始化链

init() (Once 保证幂等)
→ setup\_logging()
→ 读 DYN\_LOG 环境变量构建 EnvFilter
→ 若 OTEL\_EXPORT\_ENABLED=1 → 创建 SdkTracerProvider + OTLP exporter
→ 安装 tracing-opentelemetry layer
→ 安装 DistributedTraceIdLayer — 每 span 自动注入 trace\_id/span\_id

### 图解：跨进程 Trace 传播

<!-- SVG diagram: 一个请求的完整 Trace 链路 — 一个请求的完整 Trace 链路 · 客户端 · HTTP Request -->


## 2. Request Trace 请求追踪 — 审计日志

Request Trace 和分布式 Tracing 不同。Tracing 回答"这个请求经过了哪些组件、每个组件花了多久"，Request Trace 回答"这个请求的输入输出是什么、性能如何"。大白话：Tracing 是"请求路径图"，Request Trace 是"请求成绩单"。它记录每个请求的完整元数据快照 — 输入输出 token 数、TTFT、ITL、KV 命中率、队列深度等。数据通过广播 bus 分发到多个 sink（文件、NATS、OTLP、S3），每个 sink 一个独立 tokio 任务，互不阻塞。

### 配置与使用

#### 2A. 环境变量

| 环境变量 | 默认 | 说明 | 大白话 |
| --- | --- | --- | --- |
| `DYN_REQUEST_TRACE` | `0` | 设为 `1` 开启追踪 | "要不要记录请求成绩单" |
| `DYN_REQUEST_TRACE_SINKS` | `file` | `file,stderr,nats,otel,s3`，逗号分隔 | "记录存到哪" |
| `DYN_REQUEST_TRACE_RECORDS` | `request_end,tool` | 记录类型 | "记录哪些事件" |
| `DYN_REQUEST_TRACE_FILE_FORMAT` | `jsonl_gz` | 可选 `jsonl` / `jsonl_gz` | "文件格式：纯文本还是压缩" |
| `DYN_REQUEST_TRACE_FILE_PATH` | `/tmp/request_trace.jsonl.gz` | 文件输出路径 | "文件存哪" |

#### 2B. 开启方法

```
# 最小配置：开启追踪，输出到本地文件
export DYN_REQUEST_TRACE=1
export DYN_REQUEST_TRACE_SINKS=file
export DYN_REQUEST_TRACE_FILE_PATH=/var/log/dynamo/request_trace.jsonl.gz

# 完整配置：同时输出到文件 + NATS + OTLP
export DYN_REQUEST_TRACE=1
export DYN_REQUEST_TRACE_SINKS=file,nats,otel
export DYN_REQUEST_TRACE_RECORDS=request_end,tool,request_payload
export DYN_REQUEST_TRACE_FILE_FORMAT=jsonl_gz

# 启动 Dynamo
python -m dynamo.frontend --router-mode kv
```

### 数据模型

```
struct RequestTraceRecord {
    event_type: RequestTraceEventType,  // RequestEnd | ToolStart | ToolEnd | RequestPayload
    request:   Option<RequestTraceMetrics>,
    tool:      Option<RequestTraceToolEvent>,
    payload:   Option<RequestTracePayload>,
}
// 核心字段: request_id, model, input/output_tokens, ttft_ms, itl_ms,
//           kv_hit_rate, queue_depth, worker, finish_reason
```

### 架构 — 广播 Bus + 独立 Sink 任务

TelemetryBus<RequestTraceRecord> // broadcast, capacity=1024
├──→ tokio task → JsonlGzipSink // 1MB buffer, 1s flush, 256MB roll
├──→ tokio task → NatsSink // 发送到 NATS 主题
├──→ tokio task → OtelSink // OTLP 导出到追踪后端
└──→ tokio task → S3Sink // 上传到对象存储

### Request Trace vs Tracing vs Metrics

|  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 维度 | Metrics || Tracing | Request Trace |
| 大白话 | "仪表盘" — 聚合统计 | "路径图" — 请求链路 | "成绩单" — 逐请求审计 |
| 粒度 | 聚合（直方图/计数器） | 单请求（span 树） | 单请求（完整元数据） |
| 存储 | Prometheus（时序 DB） | Tempo（追踪 DB） | 文件/NATS/S3（原始数据） |
| 用途 | 实时监控、告警、扩缩容 | 调试延迟、定位瓶颈 | 审计、数据分析、计费 |
| 开销 | 极低（计数/打桶） | 中等（span 创建/导出） | 较高（逐请求记录） |

