//! 存储池 agent。
//!
//! P3:Dispatch 占位。P4.3:PutEnd COMPLETE + `TierPipeline`/`apply_location_events`。
//! P4.4:`TransferService` 由 `main` 挂 `lake_transfer::TransferServer`(TcpTransport)。
//! P4.8:defrag moves enqueue + `Moved` → relocate_in_view。
//! P6.4:`ReportLoad` 落地——收 Router 负载上报逐条 ack,ack 携带最近一次写路径
//! 触硬配额的 `BackpressureSignal`(TTL 内新鲜),回传 Router 做池间流控。
//! 参考:Mooncake PutEnd / transfer-engine;`hiradix_cache.py::_evict_write_back`/`lock_ref`;
//! Dynamo `offload/pipeline.rs` settlement→presence。

mod cp_port;
mod putend;

use std::pin::Pin;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tonic::{Request, Response, Status, Streaming};

pub use cp_port::{
    apply_location_events, enqueue_defrag_moves, sync_background_pause, AuthorityPort,
    ControlPlanePort,
};
pub use lake_proto::lake::*;
pub use putend::{PendingBlock, PutEndSession};

use agent_service_server::AgentService;

/// 已接受的 Dispatch 计数(冒烟/观测用)。
static DISPATCH_COUNT: AtomicU64 = AtomicU64::new(0);

pub fn dispatch_count() -> u64 {
    DISPATCH_COUNT.load(Ordering::Relaxed)
}

/// P6.4:最近一次写路径触硬配额的背压信号(进程级观测态,与 DISPATCH_COUNT 同风格)。
/// 写路径(cp_port `RejectedHardQuota`)记录;`ReportLoad`/`Dispatch` 的 Ack 携带,
/// 回传 Router 暂停该 model 新启动(池间流控;shedding 归 gateway,agent 不拒请求)。
static LAST_BACKPRESSURE: Mutex<Option<(BackpressureSignal, Instant)>> = Mutex::new(None);

/// Ack 携带背压的新鲜度窗口:超过即认为配额压力已缓解(池无主动"解除"信号,
/// 心跳 TTL 语义与 Router 侧 `SetBackpressure` 的 bpTTL 对齐)。
pub const BACKPRESSURE_FRESH_TTL: Duration = Duration::from_secs(30);

pub fn record_backpressure(bp: BackpressureSignal) {
    *LAST_BACKPRESSURE.lock().unwrap_or_else(|e| e.into_inner()) = Some((bp, Instant::now()));
}

/// TTL 内新鲜的最近一次背压(过期视为已缓解)。
pub fn last_backpressure() -> Option<BackpressureSignal> {
    LAST_BACKPRESSURE
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .as_ref()
        .filter(|(_, at)| at.elapsed() < BACKPRESSURE_FRESH_TTL)
        .map(|(bp, _)| bp.clone())
}

/// 已收到的 LoadReport 总数(观测用;P7 对接 Bifrost 时换成结构化转发)。
static LOAD_REPORT_COUNT: AtomicU64 = AtomicU64::new(0);

pub fn load_report_count() -> u64 {
    LOAD_REPORT_COUNT.load(Ordering::Relaxed)
}

#[derive(Default, Clone)]
pub struct Agent;

#[tonic::async_trait]
impl AgentService for Agent {
    async fn dispatch(&self, request: Request<DispatchRequest>) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        if req.target_node_id.is_empty() {
            return Err(Status::invalid_argument("target_node_id required"));
        }
        DISPATCH_COUNT.fetch_add(1, Ordering::Relaxed);
        Ok(Response::new(Ack {
            ok: true,
            err: String::new(),
            backpressure: last_backpressure(),
        }))
    }

    type ReportLoadStream =
        Pin<Box<dyn tokio_stream::Stream<Item = Result<Ack, Status>> + Send + 'static>>;

    async fn report_load(
        &self,
        request: Request<Streaming<LoadReport>>,
    ) -> Result<Response<Self::ReportLoadStream>, Status> {
        // P6.4:收 Router 负载快照(队列/in-flight/剩余容量),逐条 ack;
        // ack 携带最近一次写路径触硬配额的背压(TTL 内),回传 Router 做池间流控。
        let mut inbound = request.into_inner();
        let (tx, rx) = mpsc::channel(8);
        tokio::spawn(async move {
            loop {
                match inbound.message().await {
                    Ok(Some(_report)) => {
                        LOAD_REPORT_COUNT.fetch_add(1, Ordering::Relaxed);
                        let ack = Ack {
                            ok: true,
                            err: String::new(),
                            backpressure: last_backpressure(),
                        };
                        if tx.send(Ok(ack)).await.is_err() {
                            break;
                        }
                    }
                    Ok(None) => break, // 客户端正常关流
                    Err(_) => break,   // 流错误:关 ack 流,客户端负责重连
                }
            }
        });
        let stream: Self::ReportLoadStream = Box::pin(ReceiverStream::new(rx));
        Ok(Response::new(stream))
    }

    async fn place_blocks(
        &self,
        _request: Request<PlaceBlocksRequest>,
    ) -> Result<Response<Ack>, Status> {
        Ok(Response::new(Ack {
            ok: true,
            err: String::new(),
            backpressure: None,
        }))
    }
}

/// 计算节点侧能力占位(FFI / mirror / block table / fence / slot)。
#[cfg(feature = "compute")]
pub mod compute {
    pub const ROLE: &str = "compute";
}

/// KV Node 侧能力占位(NVMe serve / bounce)。
#[cfg(feature = "kvnode")]
pub mod kvnode {
    pub const ROLE: &str = "kvnode";
}

// 编译期锚定:引用具体生成符号(与 PR #18 一致)。
#[allow(dead_code)]
type _AgentServer = lake_proto::lake::agent_service_server::AgentServiceServer<()>;
#[allow(dead_code)]
type _TransferServer =
    lake_proto::lake::transfer_service_server::TransferServiceServer<lake_transfer::TransferServer>;
#[allow(dead_code)]
const _ANCHOR: fn() = || {
    let _ = DispatchRequest::default();
    let _ = PullRequest::default();
    let _ = PublishRequest::default();
};

#[cfg(test)]
mod tests {
    use super::*;
    use lake_proto::lake::agent_service_client::AgentServiceClient;
    use lake_proto::lake::agent_service_server::AgentServiceServer;

    fn bp(model: &str) -> BackpressureSignal {
        BackpressureSignal {
            model_id: model.into(),
            revision: String::new(),
            used_bytes: 200,
            soft_bytes: 100,
            hard_bytes: 150,
            deficit_bytes: 50,
            reason: "HARD_QUOTA".into(),
        }
    }

    #[test]
    fn record_then_read_backpressure() {
        record_backpressure(bp("m1"));
        let got = last_backpressure().expect("fresh bp");
        assert_eq!(got.model_id, "m1");
        assert_eq!(got.reason, "HARD_QUOTA");
        assert_eq!(got.deficit_bytes, 50);
    }

    // 端到端(真 TCP):client 流式上报 LoadReport → 逐条 ack;写路径触硬后
    // 后续 ack 携带背压(回传 Router 池间流控的回合)。
    // 注:LAST_BACKPRESSURE 是进程态(与 record_then_read_backpressure 共享),
    // 故只断言"记录后必携带",不断言"记录前不携带"。
    #[tokio::test]
    async fn report_load_acks_carry_backpressure() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            tonic::transport::Server::builder()
                .add_service(AgentServiceServer::new(Agent))
                .serve_with_incoming(tokio_stream::wrappers::TcpListenerStream::new(listener))
                .await
                .unwrap();
        });
        let mut client = AgentServiceClient::connect(format!("http://{addr}"))
            .await
            .unwrap();

        // mpsc 驱动请求流:发一条等一条 ack,消除"服务端已预先 ack"竞态
        let (tx, rx) = mpsc::channel(4);
        let stream = ReceiverStream::new(rx);
        let mut acks = client.report_load(stream).await.unwrap().into_inner();
        let report = |q: u32| LoadReport {
            node_id: "router".into(),
            queue_len: q,
            in_flight: 1,
            remaining_cap: 0,
        };

        tx.send(report(1)).await.unwrap();
        let ack1 = acks.message().await.unwrap().expect("ack1");
        assert!(ack1.ok);

        record_backpressure(bp("m-loadsync"));
        tx.send(report(2)).await.unwrap();
        let ack2 = acks.message().await.unwrap().expect("ack2");
        assert_eq!(
            ack2.backpressure.as_ref().map(|b| b.model_id.as_str()),
            Some("m-loadsync"),
            "记录背压后的 ack 必须携带信号"
        );
        drop(tx);
        assert!(load_report_count() >= 2);
    }
}
