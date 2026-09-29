# Metrics 指标与 Prometheus 接入

> 专题索引:[README.md](README.md) · 代码级剖析,基于 `3rdparty/dynamo`

## 1. Metrics 指标

Dynamo 的指标系统有一个四层层次结构：DRT → Namespace → Component → Endpoint。你每创建一个 metric，系统会自动加上 namespace、component、endpoint、worker\_id 四个标签，你不需要手动写。Prometheus 来抓取 /metrics 时，系统先运行回调函数更新 gauge 值，然后把所有子注册表的指标合并去重，输出 Prometheus 文本格式。注意：整个系统 server（/metrics、/health、/live）默认关闭，需设置 `DYN_SYSTEM_PORT >= 0` 才会开启。

### 四层层次结构 + 自动标签

DRT (DistributedRuntime)
└── Namespace → 注入 dynamo\_namespace
└── Component → 注入 dynamo\_component
└── Endpoint → 注入 dynamo\_endpoint + worker\_id

### 图解：/metrics 抓取流程

<!-- SVG diagram: Prometheus 抓取 /metrics 的完整流程 (prometheus_expfmt_combined) — Prometheus 抓取 /metrics 的完整流程 (prometheus_expfmt_combined) · Prometheus · Scrape /metrics -->

### 关键 Metric 前缀 (prometheus\_names.rs, 1238 行)

| 前缀 | 核心指标 | 大白话 |
| --- | --- | --- |
| `dynamo_frontend_` | requests\_total, ttft, itl, kv\_hit\_rate | 前端：请求数、延迟、命中率 |
| `dynamo_router_` | router\_ttft, router\_kv\_hit\_rate | 路由：路由维度延迟和命中率 |
| `dynamo_tokio_` | worker\_busy\_ratio, global\_queue\_depth | 运行时：Tokio worker 忙度、队列深度 |
| `dynamo_routing_overhead_` | block\_hashing\_ms, scheduling\_ms | 路由开销：各阶段耗时 |
| `dynamo_planner_` | num\_prefill\_replicas, observed\_ttft\_ms | Planner：当前副本数、观测到的延迟 |

## 2. 接入 Prometheus — 从零到一的完整步骤

接入 Prometheus 的"大白话"版：Dynamo 每个组件启动时都会自带一个 /metrics HTTP 端点，就像每家店门口摆了个"营业数据牌"。Prometheus 是个"巡逻员"，定时来各家店门口看一眼数据牌，把数据抄下来存到自己的数据库里。Grafana 是个"看板"，从 Prometheus 读取数据画成图表。你只需要做三件事：① 开启并确认 Dynamo 的 /metrics 端口能访问；② 告诉 Prometheus 去哪里看数据牌；③ 在 Grafana 里导入仪表板。

⚠️ **关键前提：/metrics 默认是关闭的！**  
`DYN_SYSTEM_PORT` 的默认值是 `-1`（禁用），所以直接 `python -m dynamo.vllm` 启动时 **不会** 启动 system status server，也就没有 `/metrics`、`/health`、`/live`。  
必须设置 `DYN_SYSTEM_PORT=<端口号>`（>=0）才会开启。例如 `DYN_SYSTEM_PORT=8081`。

### 第一步：开启 /metrics 端点

每个 Dynamo 组件（frontend、backend worker、router）启动时，如果设置了 `DYN_SYSTEM_PORT >= 0`，就会自动启动一个 axum HTTP 服务器，暴露三个端点：`/metrics`（指标数据）、`/health`（健康检查）、`/live`（存活检查）。worker 之间端口不冲突，因为每个 worker 设了不同的 DYN\_SYSTEM\_PORT。

```
# ✅ 正确：设置 DYN_SYSTEM_PORT 才会开启 /metrics
DYN_SYSTEM_PORT=8081 python -m dynamo.vllm --model $MODEL

# ❌ 错误：不设置 DYN_SYSTEM_PORT，/metrics 不可用
python -m dynamo.vllm --model $MODEL

# Headless 模式例外：即使设了 DYN_SYSTEM_PORT 也不开启（绕过 DistributedRuntime）
DYN_SYSTEM_PORT=8081 python -m dynamo.vllm --model $MODEL --headless  # ❌ /metrics 不可用
```

#### 端点启动链

system\_status\_server.rs (axum HTTP server 初始化)
→ 创建 TcpListener 绑定 `0.0.0.0:{DYN_SYSTEM_PORT}`
→ 注册路由: `GET /metrics`, `GET /health`, `GET /live`
→ 后台 tokio task 持续监听

#### /metrics 请求处理链

`GET /metrics` → metrics\_handler(state) (system\_status\_server.rs:336)
→ state.drt().metrics() 获取 MetricsRegistry 根注册表
→ prometheus\_expfmt() (metrics.rs)
→ ① 收集所有 child registry（Namespace → Component → Endpoint 层次树）
→ ② 运行 UpdateCallback（实时计算 gauge 值，如当前 inflight 请求数）
→ ③ FamilyMerger 去重合并（按 "name|key=value" 构建唯一键，防止重复）
→ ④ prometheus-text-encode 编码为 Prometheus 标准文本格式
→ ⑤ 追加 ExpositionFormatCallback 自定义文本
→ HTTP 200 + `text/plain; charset=utf-8`

#### 验证端点

```
# 验证 frontend 的 /metrics 端点
curl http://localhost:8000/metrics | head -20

# 验证 backend worker 的 /metrics 端点
curl http://localhost:8081/metrics | head -20

# 预期输出示例:
# HELP dynamo_frontend_requests_total Total HTTP requests
# TYPE dynamo_frontend_requests_total counter
dynamo_frontend_requests_total{model="Qwen2.5-7B",worker_id="frontend-0",finish_reason="stop"} 1523
dynamo_frontend_requests_total{model="Qwen2.5-7B",worker_id="frontend-0",finish_reason="length"} 47

# HELP dynamo_frontend_time_to_first_token_seconds Time to first token
# TYPE dynamo_frontend_time_to_first_token_seconds histogram
dynamo_frontend_time_to_first_token_seconds_bucket{le="0.1"} 0
dynamo_frontend_time_to_first_token_seconds_bucket{le="0.25"} 120
...
```

### 第二步：配置 Prometheus 抓取

Prometheus 需要一个配置文件来知道"去哪里拉数据、多久拉一次"。Dynamo 项目自带了一份开箱即用的配置文件，你只需要修改 targets 地址为你实际的组件地址。

#### 2A. 开发环境 — 静态配置

开发时每个组件的地址固定，直接写死即可。如果你在 Docker 里运行 Prometheus，用 `host.docker.internal` 可以访问宿主机的端口。


<details><summary>▶ 开发环境 prometheus.yml 完整配置</summary>

```
# dynamo/dev/observability/prometheus.yml
global:
  scrape_interval: 15s       # 全局默认 15s 拉一次
  evaluation_interval: 15s   # 规则评估间隔

scrape_configs:
  - job_name: "dynamo-frontend"
    metrics_path: /metrics
    static_configs:
      - targets: ["host.docker.internal:8000"]

  - job_name: "dynamo-backend"
    static_configs:
      - targets: ["host.docker.internal:8081", "host.docker.internal:8082"]
        labels:
          worker_type: "backend"

  - job_name: "dynamo-nixl"        # NIXL 数据传输指标
    static_configs:
      - targets: ["host.docker.internal:19090"]

  - job_name: "kvbm-metrics"        # KV Block Manager 指标
    static_configs:
      - targets: ["host.docker.internal:6880"]

  - job_name: "nats-prometheus-exporter"  # NATS 消息中间件指标
    static_configs:
      - targets: ["host.docker.internal:7777"]

  - job_name: "etcd-server"         # etcd 存储指标
    static_configs:
      - targets: ["host.docker.internal:2379"]

  - job_name: "dcgm-exporter"       # GPU 硬件指标
    static_configs:
      - targets: ["host.docker.internal:9401"]
```

</details>


#### 2B. 生产环境 K8s — ServiceMonitor

在 K8s 上不需要手动写每个 Pod 的地址 — 用 Prometheus Operator 的 ServiceMonitor，Prometheus 会自动发现所有带标签的 Pod。原理是：ServiceMonitor 通过 label selector 匹配 Service，Service 匹配 Pod，Prometheus Operator 自动把这些 Pod 的 IP:Port 生成 scrape 配置。

```
# 生产环境使用 Prometheus Operator ServiceMonitor
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: dynamo-monitor
  labels:
    release: prometheus    # 必须与 Prometheus Operator 的 release name 匹配
spec:
  selector:
    matchLabels:
      app: dynamo
  endpoints:
    - port: metrics       # 对应 Pod 的 DYN_SYSTEM_PORT
      interval: 10s
      path: /metrics
  namespaceSelector:
    matchNames: [dynamo]

# 对应的 Service 需要这样定义:
apiVersion: v1
kind: Service
metadata:
  name: dynamo-backend-svc
  labels:
    app: dynamo
spec:
  ports:
    - name: metrics
      port: 8081
      targetPort: 8081
  selector:
    app: dynamo-backend
```

#### 2C. 裸金属/虚拟机 — 服务发现脚本

如果不是 K8s 也不想手动写配置文件，可以用"文件服务发现"：写一个脚本定期生成 JSON 文件列出所有 target，Prometheus 会自动读取。这样新增/移除 worker 时只需更新文件。

```
# prometheus.yml - 文件服务发现
scrape_configs:
  - job_name: "dynamo-backend"
    file_sd_configs:
      - files:
          - "/etc/prometheus/dynamo-targets.json"
      refresh_interval: 30s

# /etc/prometheus/dynamo-targets.json - 由脚本定期生成
[
  {
    "targets": ["192.168.1.10:8081", "192.168.1.11:8081"],
    "labels": {"worker_type": "prefill", "env": "production"}
  },
  {
    "targets": ["192.168.1.12:8081", "192.168.1.13:8081"],
    "labels": {"worker_type": "decode", "env": "production"}
  }
]

# 生成脚本示例 (bash)
#!/bin/bash
# 通过 etcd 发现所有注册的 worker
ETCD_ENDPOINTS="http://localhost:2379"
PREFIX="v1/default/planner/"
targets=$(etcdctl --endpoints=$ETCD_ENDPOINTS get $PREFIX --prefix --print-value-only)
# 解析为 JSON 写入 /etc/prometheus/dynamo-targets.json
```

### 第三步：启动可观测性栈

#### 3A. 一键启动（推荐）

Dynamo 提供了一键启动完整可观测性栈的 Docker Compose 文件。一条命令就能启动 Prometheus + Grafana + Tempo + Loki + OTel Collector + DCGM Exporter + NATS Exporter，所有组件预配置好数据源连接。

```
# 进入 dynamo/dev/observability/ 目录
cd dynamo/dev/observability/

# 一键启动
docker compose -f docker-observability.yml up -d

# 查看各服务状态
docker compose ps

# 预期输出:
# NAME                          STATUS
# observability-prometheus-1    Up (healthy)
# observability-grafana-1       Up (healthy)
# observability-tempo-1         Up
# observability-loki-1          Up
# observability-otel-collector-1 Up
# observability-dcgm-exporter-1 Up
# observability-nats-exporter-1 Up
```

#### 3B. 可观测性栈各组件说明

| 组件 | 端口 | 作用 | 大白话 | 访问地址 |
| --- | --- | --- | --- | --- |
| Prometheus | 9090 | 时序指标数据库，定时拉取 /metrics | "数据采集员" — 定时来抄表 | http://localhost:9090 |
| Grafana | 3000 | 可视化仪表板，展示 Prometheus/Loki/Tempo 数据 | "看板" — 把数据画成图 | http://localhost:3000 (admin/admin) |
| Tempo | 3200 | 分布式链路追踪存储，接收 OTLP 数据 | "追踪数据库" — 记录请求全链路 | Grafana → Explore → Tempo |
| Loki | 3100 | 日志聚合，通过 label 索引日志 | "日志仓库" — 收集所有日志 | Grafana → Explore → Loki |
| OTel Collector | 4317/4318 | 接收 OTLP 格式 metrics/traces/logs，转发到对应后端 | "数据中转站" — 收数据分发给各系统 | 无需直接访问 |
| DCGM Exporter | 9401 | NVIDIA DCGM 的 Prometheus 指标导出器 | "GPU 监控探头" — 采集 GPU 硬件指标 | Prometheus 自动抓取 |
| NATS Exporter | 7777 | NATS 消息中间件的 Prometheus 指标 | "消息队列探头" — 监控 NATS 健康 | Prometheus 自动抓取 |

#### 3C. 最小启动（只接 Prometheus）

如果你只想看指标、暂时不关心链路追踪和日志，可以只启动 Prometheus 和 Grafana。

```
# 只启动 Prometheus
docker run -d --name prometheus \
  -p 9090:9090 \
  -v $(pwd)/dynamo/dev/observability/prometheus.yml:/etc/prometheus/prometheus.yml \
  prom/prometheus:latest

# 只启动 Grafana
docker run -d --name grafana \
  -p 3000:3000 \
  -v grafana-storage:/var/lib/grafana \
  grafana/grafana:latest

# 验证 Prometheus 是否抓取成功
curl http://localhost:9090/api/v1/targets | python3 -m json.tool

# 预期: states 包含 UP 的 target
```

#### 3D. Grafana 配置数据源

Grafana 启动后需要配置数据源。如果使用 docker-observability.yml 一键启动，数据源已经预配置好了。如果手动启动，需要在 Grafana UI 中手动添加。

| 步骤 | 操作 | 配置值 |
| --- | --- | --- |
| ① | 打开 Grafana → Configuration → Data Sources → Add data source | — |
| ② | 选择 Prometheus | — |
| ③ | URL 设置为 | `http://prometheus:9090`（Docker 网络内）或 `http://localhost:9090`（宿主机） |
| ④ | 点击 "Save & Test" | 应显示 "Data source is working" |
| ⑤ | 如需 Tempo：选择 Tempo → URL `http://tempo:3200` | — |
| ⑥ | 如需 Loki：选择 Loki → URL `http://loki:3100` | — |

#### 3E. 导入 Grafana 仪表板

Dynamo 自带了多个 Grafana 仪表板 JSON 文件，位于 `dynamo/dev/observability/grafana_dashboards/`。导入方式：Grafana → Dashboards → Import → Upload JSON file。如果使用了 docker-observability.yml 一键启动，仪表板通常会自动加载。

| 仪表板 | 文件 | 内容 | 大白话 |
| --- | --- | --- | --- |
| 主仪表板 | `dynamo.json` | 请求数、TTFT/ITL、KV 命中率、队列深度 | "总览看板" — 系统整体健康一目了然 |
| K8s Operator | `dynamo-operator.json` | DGD 状态、副本数、Reconcile 延迟 | "运维看板" — 看看 K8s 控制面在干什么 |
| 非聚合推理 | `disagg-dashboard.json` | Prefill/Decode 分离的延迟和资源使用 | "分拆推理看板" — 看 prefill 和 decode 各自表现 |
| KV Block Manager | `kvbm.json` | KV block 生命周期、L0/L1/L2 命中率 | "缓存看板" — 看看 KV cache 各层命中率 |
| SGLang | `sglang.json` | SGLang 后端特有指标 | "SGLang 专用看板" |
| GPU (DCGM) | `dcgm-metrics.json` | GPU 利用率、温度、显存、ECC 错误 | "GPU 硬件看板" — 显存、温度、利用率 |
| 故障恢复 | `failover.json` | 故障次数、恢复时间、级联删除 | "故障看板" — 看系统挂了几次、恢复多快 |
| 本地资源 | `dynamo_local_resource_monitor.json` | 裸金属部署的资源监控 | "裸金属看板" — CPU/内存/磁盘使用 |

```
# 如果仪表板未自动加载，手动导入:
# 方式 1: 通过 Grafana UI
# Grafana → Dashboards → Import → 上传 JSON 文件

# 方式 2: 通过 Docker 卷挂载（自动加载）
docker run -d --name grafana \
  -p 3000:3000 \
  -v grafana-storage:/var/lib/grafana \
  -v $(pwd)/dynamo/dev/observability/grafana_dashboards:/var/lib/grafana/dashboards \
  grafana/grafana:latest

# 方式 3: 通过 curl API 导入
curl -s -X POST http://admin:admin@localhost:3000/api/dashboards/import \
  -H "Content-Type: application/json" \
  --data-binary @dynamo/dev/observability/grafana_dashboards/dynamo.json
```

### 第四步：验证端到端数据流

启动一切之后，你需要验证数据是否正确流动。按照以下 checklist 逐项检查：

| 检查项 | 验证命令 | 预期结果 |
| --- | --- | --- |
| Dynamo /metrics 可访问 | `curl http://localhost:8081/metrics` | 返回 Prometheus 文本格式指标 |
| Prometheus 抓取成功 | 打开 http://localhost:9090 → Status → Targets | 所有 target 状态为 UP |
| Prometheus 有数据 | http://localhost:9090 → 搜索 `dynamo_frontend_requests_total` | 显示时间序列数据 |
| Grafana 数据源连通 | Grafana → Data Sources → Prometheus → Save & Test | "Data source is working" |
| Grafana 仪表板显示 | 打开导入的仪表板 | 图表有数据、非空白 |
| DCGM GPU 指标 | Prometheus → 搜索 `DCGM_FI_DEV_GPU_UTIL` | GPU 利用率数据 |

### Dynamo 双通道指标架构

Dynamo 的指标系统有两个"面"：一个是 expfmt（文本格式），用于 Prometheus HTTP 拉取；另一个是 typed（结构化数据），用于 OTLP gRPC 推送。两者底层数据源相同，但避免了把 Prometheus 文本再解析回结构化数据的反模式。你可以只接 Prometheus（用 Pull），或者同时接 OTLP（用 Push）到任意支持 OTLP 的后端。

<!-- SVG diagram: 接入 Prometheus 的两种方式 — 接入 Prometheus 的两种方式 · 方式一: Prometheus Pull（推荐） · Prometheus -->

### Python 端的指标注册

vLLM/SGLang 引擎启动时，会自动通过 PyO3 桥将 Python 的 prometheus\_client 指标注册到 Rust 端的 MetricsRegistry。一个指标同时注册到两个通道：expfmt（Prometheus 文本拉取）和 typed（OTLP 结构化推送）。

```
# dynamo/components/src/dynamo/common/utils/prometheus.py:55
def register_engine_metrics_callback(endpoint, registry, auto_labels=None, prefix=None):
    """注册引擎指标到两个表面。"""
    # 1. expfmt: 用于 Prometheus HTTP 拉取
    endpoint.metrics.register_prometheus_expfmt_callback(
        lambda: get_prometheus_expfmt(registry, prefix))
    # 2. typed: 用于 OTLP gRPC 推送
    endpoint.metrics.register_prometheus_typed_callback(
        lambda: get_prometheus_typed(registry, prefix))
```

### 可用指标速查

| 指标名 | 类型 | 含义 | 关键标签 | 大白话 |
| --- | --- | --- | --- | --- |
| `dynamo_frontend_requests_total` | Counter | 总请求数 | model, worker\_id, finish\_reason | "一共处理了多少请求" |
| `dynamo_frontend_inflight_requests` | Gauge | 当前处理中的请求数 | model, worker\_id | "现在正在处理多少请求" |
| `dynamo_frontend_time_to_first_token_seconds` | Histogram | 首 token 延迟（TTFT） | model, worker\_id | "用户等了多久看到第一个字" |
| `dynamo_frontend_inter_token_latency_seconds` | Histogram | Token 间延迟（ITL） | model, worker\_id | "两个字之间隔了多久" |
| `dynamo_component_kv_cache_hit_rate` | Gauge | KV Cache 命中率 | dp\_rank, worker\_id | "缓存命中了还是得重新算" |
| `dynamo_component_gpu_cache_usage_percent` | Gauge | GPU 缓存使用率 | dp\_rank, worker\_id | "GPU 显存里的缓存占了多少" |
| `dynamo_router_inflight_requests` | Gauge | 路由层 inflight 数 | router\_id | "路由器正在处理多少请求" |
| `dynamo_planner_num_prefill_replicas` | Gauge | 当前 Prefill 副本数 | — | "Planner 决定跑几个 prefill" |
| `dynamo_planner_observed_ttft_ms` | Gauge | 观测到的 TTFT | — | "Planner 看到的实际延迟" |
| `dynamo_tokio_worker_busy_ratio` | Gauge | Tokio worker 忙度 | — | "Rust 异步运行时忙不忙" |
| `dynamo_routing_overhead_scheduling_ms` | Histogram | 路由调度耗时 | — | "路由决策花了多久" |

