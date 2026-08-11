//! 存储控制面:位置视图权威(进程内存)。
//!
//! P4.2:Dynamo `BlockRegistry` + `PositionalRadixTree` + `InactiveIndex` 薄驱动。
//! P4.5:`RegisterModel` / `DeregisterModel`——每 `(model_id, revision)` 一命名空间。
//! P4.6:按命名空间软/硬配额 + 借用 + `BackpressureSignal` + `AdmitRegisterBlocks`。
//! P4.7:冷块 GC / 孤儿 TTL / 节点 reconcile / `CheckpointStore` 内存 mock。
//! P4.8:碎片整理计划(`TriggerDefrag`/`PauseBackground`)+ Location segment/offset。
//! P4.9:一致性哈希分片(`GetShardMap`/`JoinShardNode`/`DrainShardNode`)。
//! P6.1:`Mutex→RwLock` 读写分锁(lookup/locate/admit/defrag/shard_map 读路径并发);
//! `lookup_prefix` 改 `&self`——读路径懒修复本是死代码(register/import_snapshot
//! 均预建 handle),frequency touch 经内部同步的 registry 留在读路径(查询热度语义不变)。
//! 参考:`registry/mod.rs::register_sequence_hash`；Mooncake `ClearInvalidHandles` /
//! `put_start_discard_timeout` / `MetadataShard`；LMCache `QuotaManager`；
//! Mooncake allocator(无 compaction)。
//! 关键差异:节点级 reconcile 兜底 writeback 泄漏(非会话 TTL)；冷块留 L2/L3；
//! checkpoint 非 etcd(P6)；主动 defrag 经 BandwidthPool；分片只出迁移计划(字节 P5)。

mod authority;
mod checkpoint;
mod defrag;
mod hash_chain;
mod placement;
mod quota;
mod reconcile;
mod shard;
mod tier;
mod view;

use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock, RwLockReadGuard, RwLockWriteGuard};

use tokio::sync::{broadcast, mpsc};
use tokio_stream::wrappers::ReceiverStream;
use tonic::{Request, Response, Status, Streaming};

pub use authority::{Authority, NamespaceKey, RegisterStatus};
pub use checkpoint::{CheckpointStore, MemoryCheckpointStore};
pub use defrag::DEFAULT_DEFRAG_SLOT_BYTES;
pub use lake_proto::lake::*;
pub use reconcile::DEFAULT_ORPHAN_TTL_MS;
pub use shard::DEFAULT_VNODE_COUNT;

use control_plane_service_server::ControlPlaneService;

/// Keys needed for [`Authority::preflight_register`] / `AdmitRegisterBlocks` RPC.
struct AdmitKeys {
    model_id: String,
    revision: String,
    pool_kind: i32,
    hashes: Vec<Vec<u8>>,
}

fn admit_keys_from_request(req: &RegisterBlocksRequest) -> Result<AdmitKeys, String> {
    let model_id = req
        .blocks
        .iter()
        .find_map(|m| m.id.as_ref().map(|i| i.model_id.clone()))
        .ok_or_else(|| "AdmitRegisterBlocks: no KVBlockID".to_string())?;
    let revision = req
        .blocks
        .iter()
        .find_map(|m| m.id.as_ref().map(|i| i.revision.clone()))
        .unwrap_or_default();
    let pool_kind = req
        .blocks
        .iter()
        .find_map(|m| m.id.as_ref().map(|i| i.pool_kind))
        .unwrap_or(PoolKind::Target as i32);
    let hashes: Vec<Vec<u8>> = req
        .blocks
        .iter()
        .filter_map(|m| m.id.as_ref().map(|i| i.block_hash.clone()))
        .collect();
    if hashes.is_empty() {
        return Err("AdmitRegisterBlocks: no block hashes".into());
    }
    Ok(AdmitKeys {
        model_id,
        revision,
        pool_kind,
        hashes,
    })
}

/// P6.2: 视图广播通道容量。慢订阅者 Lagged 后由订阅任务快照重同步;
/// 断线订阅者的 gap 由 `view::ViewLog`(replay buffer)兜底。
pub const DEFAULT_VIEW_BROADCAST_CAP: usize = 256;

/// P7 收口:单次扩容 warmup 选块上限(对齐 Router 热集窗口量级)。
pub const DEFAULT_WARMUP_K: usize = 256;

/// 池侧 warmup 出口(方案 Z:扩容 warmup 由池自主发起,Router 只经
/// `ReportHits` 报告命中、不指挥放置)。生产接目标节点 agent `PlaceBlocks`
/// 客户端;原型默认日志实现(真实字节搬运归 P5),测试注入记录器。
///
/// 投递语义(原型边界,生产化前需重估):`warm` 无返回值、无投递确认——
/// 计划一生成就已写入 `placement_marks`(滞回),投递失败/丢失不会重试也不会
/// 清 mark,对应块在滞回窗口内不会再触发对同节点的放置。生产化需确认语义
/// (ack / 失败清 mark / 有界重试)。调用点(register_blocks/join_shard_node/
/// report_hits)一律在 Authority 写锁释放后调用,实现可以回调 CP 或等待 agent。
pub trait WarmupSink: Send + Sync {
    fn warm(&self, target_node_id: &str, ids: Vec<KvBlockId>);
}

struct LogWarmup;
impl WarmupSink for LogWarmup {
    fn warm(&self, target_node_id: &str, ids: Vec<KvBlockId>) {
        tracing::info!(
            target_node = target_node_id,
            blocks = ids.len(),
            "warmup plan (bytes P5)"
        );
    }
}

/// S3(issue #74):自动 defrag 计划的出口(与 `WarmupSink` 同型)。
/// 生产接执行侧(agent 按 moves 搬字节 + `RelocateBlocks` 回报,带宽池节流归 P5);
/// 原型默认日志实现,测试注入记录器。
pub trait DefragSink: Send + Sync {
    fn defrag_plan(&self, model_id: &str, revision: &str, pool_kind: i32, moves: Vec<DefragMove>);
}

struct LogDefrag;
impl DefragSink for LogDefrag {
    fn defrag_plan(&self, model_id: &str, revision: &str, pool_kind: i32, moves: Vec<DefragMove>) {
        tracing::info!(
            model_id,
            revision,
            pool_kind,
            moves = moves.len(),
            "auto defrag plan (bytes P5)"
        );
    }
}

#[derive(Clone)]
pub struct ControlPlane {
    /// P6.1: `Mutex → RwLock`——lookup/locate/admit/defrag/shard_map 读路径
    /// 并发（读多写少）；register/report_ref/reconcile/restore 等写路径仍互斥，
    /// 单写者权威语义不变。
    inner: Arc<RwLock<Authority>>,
    checkpoints: Arc<MemoryCheckpointStore>,
    /// Shared pause flag for promote/demote/GC/defrag (agent syncs BandwidthPool).
    background_paused: Arc<Mutex<bool>>,
    authority_poisoned_reported: Arc<AtomicBool>,
    /// P6.2: SubscribeView 直播通道(写 handler 提交事件后广播)。
    view_tx: broadcast::Sender<ViewUpdate>,
    /// P7 收口:扩容 warmup 池侧出口。
    warmup_sink: Arc<dyn WarmupSink>,
    /// S3(issue #74):自动 defrag 阈值(L2 碎片率,None=关,原型保守默认)。
    auto_defrag_threshold: Option<f64>,
    /// S3:自动 defrag 计划出口。
    defrag_sink: Arc<dyn DefragSink>,
}

impl Default for ControlPlane {
    fn default() -> Self {
        let (view_tx, _) = broadcast::channel(DEFAULT_VIEW_BROADCAST_CAP);
        Self {
            inner: Arc::new(RwLock::new(Authority::default())),
            checkpoints: Arc::new(MemoryCheckpointStore::default()),
            background_paused: Arc::new(Mutex::new(false)),
            authority_poisoned_reported: Arc::new(AtomicBool::new(false)),
            view_tx,
            warmup_sink: Arc::new(LogWarmup),
            auto_defrag_threshold: None,
            defrag_sink: Arc::new(LogDefrag),
        }
    }
}

impl ControlPlane {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_checkpoint_store(store: MemoryCheckpointStore) -> Self {
        let (view_tx, _) = broadcast::channel(DEFAULT_VIEW_BROADCAST_CAP);
        Self {
            inner: Arc::new(RwLock::new(Authority::default())),
            checkpoints: Arc::new(store),
            background_paused: Arc::new(Mutex::new(false)),
            authority_poisoned_reported: Arc::new(AtomicBool::new(false)),
            view_tx,
            warmup_sink: Arc::new(LogWarmup),
            auto_defrag_threshold: None,
            defrag_sink: Arc::new(LogDefrag),
        }
    }

    pub fn with_warmup_sink(mut self, sink: Arc<dyn WarmupSink>) -> Self {
        self.warmup_sink = sink;
        self
    }

    /// S3:配置自动 defrag 阈值(`Some(0..=1)` L2 碎片率;`None` 关闭)。
    pub fn with_auto_defrag_threshold(mut self, threshold: Option<f64>) -> Self {
        self.auto_defrag_threshold = threshold.filter(|t| (0.0..=1.0).contains(t));
        self
    }

    pub fn with_defrag_sink(mut self, sink: Arc<dyn DefragSink>) -> Self {
        self.defrag_sink = sink;
        self
    }

    pub fn background_paused(&self) -> bool {
        *self.background_paused.lock().unwrap()
    }

    pub fn set_background_paused(&self, paused: bool) {
        *self.background_paused.lock().unwrap() = paused;
    }

    pub fn authority_poisoned_reported(&self) -> bool {
        self.authority_poisoned_reported.load(Ordering::Relaxed)
    }
}

impl ControlPlane {
    fn note_lock_poisoned(&self) {
        if !self
            .authority_poisoned_reported
            .swap(true, Ordering::Relaxed)
        {
            tracing::warn!(
                event = "mutex_poisoned",
                component = "controlplane",
                lock = "authority",
                action = "return_internal_until_restart",
            );
        }
    }

    fn read_authority(&self) -> Result<RwLockReadGuard<'_, Authority>, ()> {
        self.inner.read().map_err(|_| self.note_lock_poisoned())
    }

    fn write_authority(&self) -> Result<RwLockWriteGuard<'_, Authority>, ()> {
        self.inner.write().map_err(|_| self.note_lock_poisoned())
    }

    fn lock_authority_status(_: ()) -> Status {
        Status::internal("controlplane authority unavailable")
    }

    /// P6.2: 提交本写调用累积的视图事件并广播(写锁内调用,提交序=广播序)。
    /// 无论 RPC 成败都调用:事件反映状态迁移,部分变异也要让镜像看到。
    fn commit_and_broadcast(&self, auth: &mut Authority) {
        if let Some(u) = auth.commit_view_events() {
            // 无订阅者时 send 返回 Err,忽略;慢订阅者 Lagged 由订阅任务快照重同步。
            let _ = self.view_tx.send(u);
        }
    }

    /// S3(issue #74):配置门控的自动 defrag 评估(当前挂 register_blocks 写路径——
    /// L2 布局随注册增长的主入口;relocate/evict 路径的评估挂点留待生产化)。
    ///
    /// 碎片率 ≥ 阈值 → 生成 defrag plan 经 `DefragSink` 下发。遵守
    /// `PauseBackground` 暂停语义(与 promote/demote/GC 共享后台暂停旗标;
    /// 字节级带宽节流归 P5 `BandwidthPool`,此处只出计划)。
    /// 已知原型边界:计划下发到 agent 执行 `RelocateBlocks` 回报之间,后续
    /// register 会重复触发同碎段计划——执行幂等(同坐标 relocate 为坐标刷新),
    /// 生产化时由执行侧去重/节流。
    /// 挂点边界(PR #77 review):评估只挂 `register_blocks` 写路径——稳态触发
    /// 依赖 register 频次;只 relocate/evict 不再 re-register 的负载永不触发,
    /// relocate/evict 路径的评估挂点留生产化。
    /// 锁内只**生成**计划:返回 `(model_id, revision, pool_kind, moves)` 供锁外
    /// 投递。DefragSink 是外部边界(生产实现可能等待 agent 或回调 CP 如 Locate/
    /// 迁移回报)——写锁内同步调用会阻塞全局 CP 读写,回调即死锁(PR #77 review)。
    fn plan_auto_defrag(
        &self,
        auth: &Authority,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
    ) -> Option<(String, String, i32, Vec<DefragMove>)> {
        let threshold = self.auto_defrag_threshold?;
        if self.background_paused() {
            return None;
        }
        let ratio = auth.l2_fragmentation_ratio(model_id, revision, pool_kind, 0);
        if ratio < threshold {
            return None;
        }
        match auth.plan_defrag(model_id, revision, pool_kind, DefragMode::Both, 0) {
            Ok(moves) if !moves.is_empty() => {
                Some((model_id.to_string(), revision.to_string(), pool_kind, moves))
            }
            _ => None,
        }
    }
}

/// P6.2: 锚点更新(空事件):快照/重放之后告知接收方当前权威 seq。
/// seq=0 返回 None(不发):快照(seq=0,重置语义)之后接收方 lastSeq 已为 0,
/// 零锚点无信息量;且接收方把 seq==0 一律按快照重置处理(见 Go ViewMirror),
/// 零锚点会把刚应用的非空快照清空——全新进程 import_snapshot 恢复非空权威后
/// view_last_seq()==0 正是该场景。
fn anchor_update(seq: u64) -> Option<ViewUpdate> {
    (seq > 0).then_some(ViewUpdate {
        seq,
        events: Vec::new(),
    })
}

/// 快照 + 锚点(last>0 时)的初始序列与锚点 seq,供 subscribe_view 两个快照分支共用。
fn snapshot_with_anchor(auth: &Authority) -> (Vec<ViewUpdate>, u64) {
    let last = auth.view_last_seq();
    let mut initial = vec![auth.view_snapshot()];
    if let Some(anchor) = anchor_update(last) {
        initial.push(anchor);
    }
    (initial, last)
}

#[tonic::async_trait]
impl ControlPlaneService for ControlPlane {
    type SubscribeViewStream =
        Pin<Box<dyn tokio_stream::Stream<Item = Result<ViewUpdate, Status>> + Send + 'static>>;

    /// P6.2: snapshot-on-connect / resume 重放 / 增量直播。
    ///
    /// 协议(见 `view.rs` 头注释):先 subscribe broadcast 再取读锁做快照——读锁
    /// 释放后的提交 seq 必大于锚点且经 broadcast 送达,重叠部分按 `seq <= last`
    /// 去重(借鉴 Dynamo `DeduplicatingStream`,单权威退化为单流序号)。
    async fn subscribe_view(
        &self,
        request: Request<SubscribeRequest>,
    ) -> Result<Response<Self::SubscribeViewStream>, Status> {
        let req = request.into_inner();
        tracing::debug!(
            subscriber_id = %req.subscriber_id,
            resume_from_seq = req.resume_from_seq,
            "subscribe_view connected"
        );
        let mut rx = self.view_tx.subscribe();
        let (initial, anchor) = {
            let auth = self.read_authority().map_err(Self::lock_authority_status)?;
            if req.resume_from_seq == 0 {
                snapshot_with_anchor(&auth)
            } else {
                match auth.replay_view_after(req.resume_from_seq) {
                    Some(ups) => {
                        let anchor = ups.last().map(|u| u.seq).unwrap_or(req.resume_from_seq);
                        (ups, anchor)
                    }
                    // replay buffer 已逐出 → 回退快照(有界 buffer 语义)
                    None => snapshot_with_anchor(&auth),
                }
            }
        };
        let (tx, out) = mpsc::channel(64);
        let inner = Arc::clone(&self.inner);
        tokio::spawn(async move {
            for u in initial {
                if tx.send(Ok(u)).await.is_err() {
                    return;
                }
            }
            let mut last = anchor;
            loop {
                match rx.recv().await {
                    Ok(u) => {
                        if u.seq == 0 {
                            // restore 广播的快照(重置语义)不是订阅时的快照/广播重叠,
                            // 必须转发;dedup 水位归零,后续新世代增量照常经下方去重。
                            // 否则在线订阅者永远收不到权威重建,镜像保留旧状态发散。
                            last = 0;
                            if tx.send(Ok(u)).await.is_err() {
                                return;
                            }
                            continue;
                        }
                        if u.seq <= last {
                            continue; // 去重:快照/广播重叠
                        }
                        last = u.seq;
                        if tx.send(Ok(u)).await.is_err() {
                            return;
                        }
                    }
                    Err(broadcast::error::RecvError::Lagged(_)) => {
                        // 慢订阅者:快照重同步(单写者顺序提交保证之后 seq 连续)
                        let (snap, a) = match inner.read() {
                            Ok(auth) => (auth.view_snapshot(), auth.view_last_seq()),
                            Err(_) => return, // poisoned:让客户端重连走 resume
                        };
                        if tx.send(Ok(snap)).await.is_err() {
                            return;
                        }
                        if let Some(anchor) = anchor_update(a) {
                            if tx.send(Ok(anchor)).await.is_err() {
                                return;
                            }
                        }
                        last = a;
                    }
                    Err(broadcast::error::RecvError::Closed) => return,
                }
            }
        });
        Ok(Response::new(Box::pin(ReceiverStream::new(out))))
    }

    async fn lookup_prefix(
        &self,
        request: Request<LookupPrefixRequest>,
    ) -> Result<Response<LookupPrefixResponse>, Status> {
        let req = request.into_inner();
        let auth = self.read_authority().map_err(Self::lock_authority_status)?;
        let (blocks, hit_length, all_local_hit) = auth.lookup_prefix(
            &req.model_id,
            &req.revision,
            req.pool_kind,
            &req.prefix_hashes,
            &req.requester_node_id,
        );
        Ok(Response::new(LookupPrefixResponse {
            blocks,
            hit_length,
            all_local_hit,
        }))
    }

    async fn locate(
        &self,
        request: Request<LocateRequest>,
    ) -> Result<Response<LocateResponse>, Status> {
        let req = request.into_inner();
        let auth = self.read_authority().map_err(Self::lock_authority_status)?;
        let blocks = auth.locate(&req.ids);
        Ok(Response::new(LocateResponse { blocks }))
    }

    async fn admit_register_blocks(
        &self,
        request: Request<RegisterBlocksRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let keys = match admit_keys_from_request(&req) {
            Ok(v) => v,
            Err(e) => {
                return Ok(Response::new(Ack {
                    ok: false,
                    err: e,
                    backpressure: None,
                }));
            }
        };
        let auth = self.read_authority().map_err(Self::lock_authority_status)?;
        match auth.preflight_register(&keys.model_id, &keys.revision, keys.pool_kind, &keys.hashes)
        {
            Ok(RegisterStatus::Accepted) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Ok(RegisterStatus::RejectedHardQuota(bp)) => {
                tracing::warn!(
                    model_id = %keys.model_id,
                    revision = %keys.revision,
                    pool_kind = keys.pool_kind,
                    reason = %bp.reason,
                    deficit_bytes = bp.deficit_bytes,
                    "admission rejected: hard quota backpressure"
                );
                Ok(Response::new(Ack {
                    ok: false,
                    err: format!(
                        "AdmitRegisterBlocks: admission rejected reason={} deficit={}",
                        bp.reason, bp.deficit_bytes
                    ),
                    backpressure: Some(bp),
                }))
            }
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }

    async fn register_blocks(
        &self,
        request: Request<RegisterBlocksRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        tracing::debug!(
            node_id = %req.node_id,
            blocks = req.blocks.len(),
            "register_blocks"
        );
        // P7.6(B2-a):HRW 预放置需链所属命名空间,register 移动 blocks 前取出。
        let ns_identity = req
            .blocks
            .iter()
            .find_map(|m| m.id.as_ref())
            .map(|i| (i.model_id.clone(), i.revision.clone(), i.pool_kind));
        // 锁内只做视图读写与计划生成;外部 sink(WarmupSink/DefragSink)一律锁外
        // 投递——实现可能等待 agent 或回调 CP,锁内调用会全局阻塞/死锁(PR #77 review)。
        let (result, preplace, defrag) = {
            let mut auth = self
                .write_authority()
                .map_err(Self::lock_authority_status)?;
            let result = auth.register(&req.node_id, &req.prefix_hashes, req.blocks);
            // P7.6(B2-a):register 成功后按 HRW(链锚点, ready 节点集)预放置
            // (稳态常见情形=生产节点即家节点,计划为空零动作)。
            let preplace = if matches!(result, Ok(RegisterStatus::Accepted)) {
                ns_identity.as_ref().and_then(|(mid, rev, pk)| {
                    auth.preplace_on_register(mid, rev, *pk, &req.prefix_hashes)
                })
            } else {
                None
            };
            // S3(issue #74):配置门控自动 defrag 评估(默认关;暂停期不触发)。
            let defrag = if matches!(result, Ok(RegisterStatus::Accepted)) {
                ns_identity
                    .as_ref()
                    .and_then(|(mid, rev, pk)| self.plan_auto_defrag(&auth, mid, rev, *pk))
            } else {
                None
            };
            self.commit_and_broadcast(&mut auth);
            (result, preplace, defrag)
        };
        if let Some((home, plan)) = preplace {
            self.warmup_sink.warm(&home, plan);
        }
        if let Some((mid, rev, pk, moves)) = defrag {
            self.defrag_sink.defrag_plan(&mid, &rev, pk, moves);
        }
        match result {
            Ok(RegisterStatus::Accepted) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Ok(RegisterStatus::RejectedHardQuota(bp)) => {
                tracing::warn!(
                    node_id = %req.node_id,
                    reason = %bp.reason,
                    deficit_bytes = bp.deficit_bytes,
                    "register_blocks rejected: hard quota backpressure"
                );
                Ok(Response::new(Ack {
                    ok: false,
                    err: format!(
                        "RegisterBlocks: admission rejected reason={} deficit={}",
                        bp.reason, bp.deficit_bytes
                    ),
                    backpressure: Some(bp),
                }))
            }
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }

    async fn report_ref(
        &self,
        request: Request<Streaming<RefDelta>>,
    ) -> Result<Response<Ack>, Status> {
        let mut stream = request.into_inner();
        let mut deltas = Vec::new();
        while let Some(delta) = stream.message().await? {
            deltas.push(delta);
        }
        tracing::debug!(deltas = deltas.len(), "report_ref");
        let mut auth = self
            .write_authority()
            .map_err(Self::lock_authority_status)?;
        match auth.report_refs(&deltas) {
            Ok(()) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Err(e) => {
                // S4:underflow/overflow 全有或全无拒绝——账本不一致信号,告警可见。
                tracing::warn!(err = %e, "report_ref rejected");
                Ok(Response::new(Ack {
                    ok: false,
                    err: e,
                    backpressure: None,
                }))
            }
        }
    }

    async fn request_barrier(
        &self,
        request: Request<RequestBarrierRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let mut auth = self
            .write_authority()
            .map_err(Self::lock_authority_status)?;
        // P4.3: agent must flush L2 + ReportRef(WRITEBACK,-1) before this call.
        match auth.complete_barrier(&req.request_id, &req.node_id) {
            Ok(()) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }

    type LeaseStream =
        Pin<Box<dyn tokio_stream::Stream<Item = Result<LeaseAck, Status>> + Send + 'static>>;

    async fn lease(
        &self,
        _request: Request<Streaming<LeaseHeartbeat>>,
    ) -> Result<Response<Self::LeaseStream>, Status> {
        let (tx, rx) = mpsc::channel(1);
        drop(tx);
        let stream: Self::LeaseStream = Box::pin(ReceiverStream::new(rx));
        Ok(Response::new(stream))
    }

    async fn register_model(
        &self,
        request: Request<RegisterModelRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let Some(model) = req.model else {
            return Ok(Response::new(Ack {
                ok: false,
                err: "RegisterModel: model required".into(),
                backpressure: None,
            }));
        };
        tracing::info!(
            model_id = %model.model_id,
            revision = %model.revision,
            "register_model"
        );
        let mut auth = self
            .write_authority()
            .map_err(Self::lock_authority_status)?;
        match auth.register_model(model) {
            Ok(()) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }

    async fn deregister_model(
        &self,
        request: Request<DeregisterModelRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        tracing::info!(
            model_id = %req.model_id,
            revision = %req.revision,
            "deregister_model"
        );
        let mut auth = self
            .write_authority()
            .map_err(Self::lock_authority_status)?;
        let result = auth.deregister_model(&req.model_id, &req.revision);
        self.commit_and_broadcast(&mut auth);
        match result {
            Ok(()) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }

    async fn set_model_quota(
        &self,
        request: Request<SetModelQuotaRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let Some(quota) = req.quota else {
            return Ok(Response::new(Ack {
                ok: false,
                err: "SetModelQuota: quota required".into(),
                backpressure: None,
            }));
        };
        let mut auth = self
            .write_authority()
            .map_err(Self::lock_authority_status)?;
        match auth.set_model_quota(&req.model_id, &req.revision, quota) {
            Ok(()) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }

    async fn get_model_quota(
        &self,
        request: Request<GetModelQuotaRequest>,
    ) -> Result<Response<GetModelQuotaResponse>, Status> {
        let req = request.into_inner();
        let auth = self.read_authority().map_err(Self::lock_authority_status)?;
        match auth.get_model_quota(&req.model_id, &req.revision) {
            Ok(resp) => Ok(Response::new(resp)),
            Err(e) => Ok(Response::new(GetModelQuotaResponse {
                quota: None,
                used_bytes: 0,
                borrowed_bytes: 0,
                backpressure: None,
                ok: false,
                err: e,
            })),
        }
    }

    async fn reconcile_orphans(
        &self,
        request: Request<ReconcileOrphansRequest>,
    ) -> Result<Response<ReconcileOrphansResponse>, Status> {
        let req = request.into_inner();
        let mut auth = self
            .write_authority()
            .map_err(Self::lock_authority_status)?;
        let result = auth.reconcile_orphans(&req);
        self.commit_and_broadcast(&mut auth);
        match result {
            Ok(resp) => {
                tracing::info!(
                    dead_node_id = %req.dead_node_id,
                    discarded = resp.discarded.len(),
                    cold_stripped = resp.cold_stripped.len(),
                    refs_cleared = resp.refs_cleared,
                    "reconcile_orphans"
                );
                if !resp.discarded.is_empty()
                    || !resp.cold_stripped.is_empty()
                    || resp.refs_cleared > 0
                {
                    tracing::warn!(
                        dead_node_id = %req.dead_node_id,
                        discarded = resp.discarded.len(),
                        cold_stripped = resp.cold_stripped.len(),
                        refs_cleared = resp.refs_cleared,
                        "reconcile found orphans/dead-node holdings"
                    );
                }
                Ok(Response::new(resp))
            }
            Err(e) => Ok(Response::new(ReconcileOrphansResponse {
                discarded: vec![],
                cold_stripped: vec![],
                refs_cleared: 0,
                ok: false,
                err: e,
            })),
        }
    }

    async fn discard_blocks(
        &self,
        request: Request<DiscardBlocksRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let mut auth = self
            .write_authority()
            .map_err(Self::lock_authority_status)?;
        let result = auth.discard_blocks(&req.ids);
        self.commit_and_broadcast(&mut auth);
        match result {
            Ok(_) => Ok(Response::new(Ack {
                ok: true,
                err: String::new(),
                backpressure: None,
            })),
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }

    async fn save_checkpoint(
        &self,
        _request: Request<SaveCheckpointRequest>,
    ) -> Result<Response<SaveCheckpointResponse>, Status> {
        let mut auth = self
            .write_authority()
            .map_err(Self::lock_authority_status)?;
        auth.checkpoint_seq = auth.checkpoint_seq.saturating_add(1);
        let snap = auth.export_snapshot(auth.checkpoint_seq);
        drop(auth);
        match self.checkpoints.save(snap.clone()) {
            Ok(()) => Ok(Response::new(SaveCheckpointResponse {
                snapshot: Some(snap),
                ok: true,
                err: String::new(),
            })),
            Err(e) => Ok(Response::new(SaveCheckpointResponse {
                snapshot: None,
                ok: false,
                err: e,
            })),
        }
    }

    async fn restore_checkpoint(
        &self,
        request: Request<RestoreCheckpointRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let snap = if let Some(s) = req.snapshot {
            s
        } else {
            match self.checkpoints.load() {
                Ok(Some(s)) => s,
                Ok(None) => {
                    return Ok(Response::new(Ack {
                        ok: false,
                        err: "RestoreCheckpoint: store empty".into(),
                        backpressure: None,
                    }));
                }
                Err(e) => {
                    return Ok(Response::new(Ack {
                        ok: false,
                        err: e,
                        backpressure: None,
                    }));
                }
            }
        };
        let mut auth = self
            .write_authority()
            .map_err(Self::lock_authority_status)?;
        match auth.import_snapshot(&snap) {
            Ok(()) => {
                // P6.2: 权威已整体重建——向在线订阅者广播新快照(seq=0 重置语义)+ 锚点;
                // 断开的订阅者 resume 时因 buffer 已清空回退快照。
                let _ = self.view_tx.send(auth.view_snapshot());
                if let Some(anchor) = anchor_update(auth.view_last_seq()) {
                    let _ = self.view_tx.send(anchor);
                }
                Ok(Response::new(Ack {
                    ok: true,
                    err: String::new(),
                    backpressure: None,
                }))
            }
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }

    async fn trigger_defrag(
        &self,
        request: Request<TriggerDefragRequest>,
    ) -> Result<Response<TriggerDefragResponse>, Status> {
        let req = request.into_inner();
        let mode = DefragMode::try_from(req.mode).unwrap_or(DefragMode::Both);
        let auth = self.read_authority().map_err(Self::lock_authority_status)?;
        match auth.plan_defrag(
            &req.model_id,
            &req.revision,
            req.pool_kind,
            mode,
            req.slot_bytes,
        ) {
            Ok(moves) => {
                let planned_moves = moves.len() as u32;
                tracing::info!(
                    model_id = %req.model_id,
                    revision = %req.revision,
                    pool_kind = req.pool_kind,
                    mode = ?mode,
                    planned_moves,
                    "trigger_defrag"
                );
                Ok(Response::new(TriggerDefragResponse {
                    moves,
                    planned_moves,
                    ok: true,
                    err: String::new(),
                }))
            }
            Err(e) => Ok(Response::new(TriggerDefragResponse {
                moves: vec![],
                planned_moves: 0,
                ok: false,
                err: e,
            })),
        }
    }

    async fn pause_background(
        &self,
        request: Request<PauseBackgroundRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        self.set_background_paused(req.paused);
        tracing::info!(paused = req.paused, "pause_background");
        Ok(Response::new(Ack {
            ok: true,
            err: String::new(),
            backpressure: None,
        }))
    }

    async fn get_shard_map(
        &self,
        _request: Request<GetShardMapRequest>,
    ) -> Result<Response<GetShardMapResponse>, Status> {
        let auth = self.read_authority().map_err(Self::lock_authority_status)?;
        Ok(Response::new(GetShardMapResponse {
            map: Some(auth.shard_map()),
            ok: true,
            err: String::new(),
        }))
    }

    async fn join_shard_node(
        &self,
        request: Request<JoinShardNodeRequest>,
    ) -> Result<Response<JoinShardNodeResponse>, Status> {
        let req = request.into_inner();
        // 锁内 join + 生成 warmup 计划;WarmupSink 锁外投递(同 register_blocks
        // 的锁外投递约束,PR #77 review)。
        let (resp, warm) = {
            let mut auth = self
                .write_authority()
                .map_err(Self::lock_authority_status)?;
            match auth.join_shard_node(&req.node_id, req.vnode_count) {
                Ok((map, migrations)) => {
                    let migration_count = migrations.len() as u32;
                    tracing::info!(
                        node_id = %req.node_id,
                        vnode_count = req.vnode_count,
                        migration_count,
                        "join_shard_node"
                    );
                    // P7 收口(方案 Z):新节点 warmup 由池侧按 hit_count 自主选块
                    // 发起;Router 只经 ReportHits 报告命中,不指挥放置。
                    let plan = auth.warmup_plan(&req.node_id, DEFAULT_WARMUP_K);
                    (
                        JoinShardNodeResponse {
                            map: Some(map),
                            migrations,
                            migration_count,
                            ok: true,
                            err: String::new(),
                        },
                        if plan.is_empty() { None } else { Some(plan) },
                    )
                }
                Err(e) => (
                    JoinShardNodeResponse {
                        map: None,
                        migrations: vec![],
                        migration_count: 0,
                        ok: false,
                        err: e,
                    },
                    None,
                ),
            }
        };
        if let Some(plan) = warm {
            self.warmup_sink.warm(&req.node_id, plan);
        }
        Ok(Response::new(resp))
    }

    async fn drain_shard_node(
        &self,
        request: Request<DrainShardNodeRequest>,
    ) -> Result<Response<DrainShardNodeResponse>, Status> {
        let req = request.into_inner();
        let mut auth = self
            .write_authority()
            .map_err(Self::lock_authority_status)?;
        match auth.drain_shard_node(&req.node_id) {
            Ok((map, migrations, push_l2)) => {
                let migration_count = migrations.len() as u32;
                Ok(Response::new(DrainShardNodeResponse {
                    map: Some(map),
                    migrations,
                    push_l2,
                    migration_count,
                    ok: true,
                    err: String::new(),
                }))
            }
            Err(e) => Ok(Response::new(DrainShardNodeResponse {
                map: None,
                migrations: vec![],
                push_l2: vec![],
                migration_count: 0,
                ok: false,
                err: e,
            })),
        }
    }

    /// P7 收口:命中上报 → radix hit_count(供 warmup 选块/预放置)。
    /// best-effort:未知块跳过,不报错;不进 ViewEvent。
    /// P7.6(B2-b):per-(block,node) 计数 + 跟随流量放置(过阈值经 WarmupSink
    /// 下发,滞回不重复;`node_id` = 命中流量的服务节点)。
    async fn report_hits(
        &self,
        request: Request<ReportHitsRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        tracing::debug!(
            node_id = %req.node_id,
            ids = req.ids.len(),
            "report_hits"
        );
        // 锁内只累计命中并生成跟随流量计划;WarmupSink 锁外投递(同
        // register_blocks 的锁外投递约束,PR #77 review)。
        let plans = {
            let mut auth = self
                .write_authority()
                .map_err(Self::lock_authority_status)?;
            auth.report_hits(&req.node_id, &req.ids)
        };
        for (node, plan) in plans {
            self.warmup_sink.warm(&node, plan);
        }
        Ok(Response::new(Ack {
            ok: true,
            err: String::new(),
            backpressure: None,
        }))
    }

    async fn remove_shard_node(
        &self,
        request: Request<RemoveShardNodeRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let mut auth = self
            .write_authority()
            .map_err(Self::lock_authority_status)?;
        match auth.remove_shard_node(&req.node_id) {
            Ok(()) => {
                tracing::info!(node_id = %req.node_id, "remove_shard_node");
                Ok(Response::new(Ack {
                    ok: true,
                    err: String::new(),
                    backpressure: None,
                }))
            }
            Err(e) => Ok(Response::new(Ack {
                ok: false,
                err: e,
                backpressure: None,
            })),
        }
    }
}

#[allow(dead_code)]
type _CpServer = lake_proto::lake::control_plane_service_server::ControlPlaneServiceServer<()>;
#[allow(dead_code)]
const _ANCHOR: fn() = || {
    let _ = RegisterBlocksRequest::default();
    let _ = LookupPrefixRequest::default();
    let _ = RegisterModelRequest::default();
    let _ = DeregisterModelRequest::default();
    let _ = SetModelQuotaRequest::default();
    let _ = GetModelQuotaRequest::default();
    let _ = BackpressureSignal::default();
    let _ = RefDelta::default();
    // AdmitRegisterBlocks reuses RegisterBlocksRequest on the wire.
    let _ = RegisterBlocksRequest::default();
};

#[cfg(test)]
mod tests {
    use super::*;
    use tokio_stream::StreamExt as _;

    fn ensure_model(auth: &mut Authority, model: &str) {
        ensure_model_rev(auth, model, "");
    }

    fn ensure_model_rev(auth: &mut Authority, model: &str, revision: &str) {
        auth.register_model(ModelDescriptor {
            model_id: model.into(),
            revision: revision.into(),
            num_layers: 32,
            block_spec: Some(BlockSpec {
                block_tokens: 128,
                bytes_per_block: 0,
            }),
            hash_algo: HashAlgo::HashSha256256 as i32,
            quota: Some(Quota {
                soft_bytes: 0,
                hard_bytes: 0,
                borrow_enabled: false,
            }),
        })
        .unwrap();
    }

    fn meta(model: &str, hash: &[u8]) -> BlockMeta {
        meta_rev(model, "", hash)
    }

    fn meta_rev(model: &str, revision: &str, hash: &[u8]) -> BlockMeta {
        BlockMeta {
            id: Some(KvBlockId {
                model_id: model.into(),
                block_hash: hash.to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
                revision: revision.into(),
            }),
            block_kind: BlockKind::TType as i32,
            locations: vec![Location {
                tier: Tier::L2 as i32,
                node_id: "n0".into(),
                segment_id: 1,
                offset: 0,
            }],
            l3_present: false,
            ref_count: 1,
        }
    }

    fn prefix(hashes: &[&[u8]]) -> Vec<Vec<u8>> {
        hashes.iter().map(|h| h.to_vec()).collect()
    }

    fn delta(model: &str, flat: &[u8], d: i32) -> RefDelta {
        delta_rev(model, "", flat, d)
    }

    fn delta_rev(model: &str, revision: &str, flat: &[u8], d: i32) -> RefDelta {
        RefDelta {
            id: Some(KvBlockId {
                model_id: model.into(),
                block_hash: flat.to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
                revision: revision.into(),
            }),
            kind: RefKind::Request as i32,
            delta: d,
            node_id: "n0".into(),
        }
    }

    #[test]
    fn lookup_prefix_contiguous_then_gap() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"h0", b"h1", b"h2"]);
        auth.register(
            "n0",
            &full,
            vec![meta("m", b"h0"), meta("m", b"h1"), meta("m", b"h2")],
        )
        .unwrap();
        let (blocks, hit, local) = auth.lookup_prefix(
            "m",
            "",
            PoolKind::Target as i32,
            &prefix(&[b"h0", b"gap", b"h2"]),
            "n0",
        );
        assert_eq!(hit, 1);
        assert_eq!(blocks.len(), 1);
        assert!(!local);
    }

    #[test]
    fn lookup_prefix_full_hit_not_local_without_l0() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"a", b"b"]);
        auth.register("n0", &full, vec![meta("m", b"a"), meta("m", b"b")])
            .unwrap();
        let (_, hit, local) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 2);
        assert!(!local);
    }

    #[test]
    fn cross_model_isolation() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m1");
        ensure_model(&mut auth, "m2");
        let full = prefix(&[b"shared"]);
        auth.register("n0", &full, vec![meta("m1", b"shared")])
            .unwrap();
        auth.register("n0", &full, vec![meta("m2", b"shared")])
            .unwrap();
        let (_, hit1, _) = auth.lookup_prefix("m1", "", PoolKind::Target as i32, &full, "n0");
        let (_, hit2, _) = auth.lookup_prefix("m2", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit1, 1);
        assert_eq!(hit2, 1);
        let (_, miss, _) = auth.lookup_prefix(
            "m1",
            "",
            PoolKind::Target as i32,
            &prefix(&[b"other"]),
            "n0",
        );
        assert_eq!(miss, 0);
    }

    #[test]
    fn register_requires_prefix_hashes() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let err = auth.register("n0", &[], vec![meta("m", b"a")]).unwrap_err();
        assert!(err.contains("prefix_hashes"));
    }

    #[test]
    fn register_requires_registered_model() {
        let mut auth = Authority::default();
        let full = prefix(&[b"a"]);
        let err = auth
            .register("n0", &full, vec![meta("m", b"a")])
            .unwrap_err();
        assert!(err.contains("not registered"));
    }

    #[test]
    fn register_miss_suffix_with_full_chain() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"h0", b"h1", b"h2"]);
        auth.register("n0", &full, vec![meta("m", b"h0"), meta("m", b"h1")])
            .unwrap();
        auth.register("n0", &full, vec![meta("m", b"h2")]).unwrap();
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 3);
    }

    #[test]
    fn register_rejects_non_contiguous_subset() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"h0", b"h1", b"h2"]);
        let err = auth
            .register("n0", &full, vec![meta("m", b"h0"), meta("m", b"h2")])
            .unwrap_err();
        assert!(err.contains("contiguous"));
    }

    #[test]
    fn lookup_stops_on_lineage_mismatch() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let chain = prefix(&[b"A", b"B"]);
        auth.register("n0", &chain, vec![meta("m", b"A"), meta("m", b"B")])
            .unwrap();
        let (blocks, hit, _) =
            auth.lookup_prefix("m", "", PoolKind::Target as i32, &prefix(&[b"B"]), "n0");
        assert_eq!(hit, 0);
        assert!(blocks.is_empty());
    }

    #[test]
    fn ref_freeze_and_evict() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"x"]);
        auth.register("n0", &full, vec![meta("m", b"x")]).unwrap();

        let d = delta("m", b"x", 1);
        auth.report_ref(&d).unwrap();
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"x"), 1);
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 0);

        let mut d0 = d.clone();
        d0.delta = -1;
        auth.report_ref(&d0).unwrap();
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"x"), 0);
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 1);

        auth.report_ref(&d).unwrap();
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 0);
        auth.report_ref(&d0).unwrap();
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 1);

        let n = auth.evict_n("m", "", PoolKind::Target as i32, 1);
        assert_eq!(n, 1);
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 0);
    }

    #[test]
    fn report_ref_batch_all_or_nothing() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"p"]);
        auth.register("n0", &full, vec![meta("m", b"p")]).unwrap();
        let plus = delta("m", b"p", 1);
        auth.report_ref(&plus).unwrap();
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"p"), 1);

        let mut minus = plus.clone();
        minus.delta = -1;
        let unknown = delta("m", b"missing", -1);
        let err = auth.report_refs(&[minus, unknown]).unwrap_err();
        assert!(err.contains("unknown block_hash") || err.contains("batch"));
        assert_eq!(
            auth.global_ref("m", "", PoolKind::Target as i32, b"p"),
            1,
            "failed batch must not apply prefix deltas"
        );
    }

    #[test]
    fn ref_gt_zero_not_evicted() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"y"]);
        auth.register("n0", &full, vec![meta("m", b"y")]).unwrap();
        auth.report_ref(&delta("m", b"y", 1)).unwrap();
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 10), 0);
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 1);
    }

    #[test]
    fn frequency_evicts_colder_leaf_first() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let cold = prefix(&[b"cold"]);
        let hot = prefix(&[b"hot"]);
        auth.register("n0", &cold, vec![meta("m", b"cold")])
            .unwrap();
        auth.register("n0", &hot, vec![meta("m", b"hot")]).unwrap();
        for _ in 0..64 {
            let _ = auth.lookup_prefix("m", "", PoolKind::Target as i32, &hot, "n0");
        }
        for flat in [b"cold".as_slice(), b"hot".as_slice()] {
            auth.report_ref(&delta("m", flat, 1)).unwrap();
            auth.report_ref(&delta("m", flat, -1)).unwrap();
        }
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 2);
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 1), 1);
        let (_, cold_hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &cold, "n0");
        let (_, hot_hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &hot, "n0");
        assert_eq!(cold_hit, 0, "colder leaf should be Frequency victim");
        assert_eq!(hot_hit, 1, "touched hot leaf should survive first allocate");
    }

    #[test]
    fn authority_evicts_leaf_before_prefix_parent() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let chain = prefix(&[b"parent", b"child"]);
        auth.register(
            "n0",
            &chain,
            vec![meta("m", b"parent"), meta("m", b"child")],
        )
        .unwrap();
        for flat in [b"parent".as_slice(), b"child".as_slice()] {
            auth.report_ref(&delta("m", flat, 1)).unwrap();
            auth.report_ref(&delta("m", flat, -1)).unwrap();
        }
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 1), 1);
        let (_, parent_only, _) = auth.lookup_prefix(
            "m",
            "",
            PoolKind::Target as i32,
            &prefix(&[b"parent"]),
            "n0",
        );
        assert_eq!(parent_only, 1, "prefix parent must survive first evict");
        let (_, chain_hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &chain, "n0");
        assert_eq!(chain_hit, 1, "leaf gone → gap after parent");
    }

    #[test]
    fn inactive_cap_skip_insert_no_zombie() {
        let cap = 4;
        let mut auth = Authority::with_inactive_cap(cap);
        ensure_model(&mut auth, "m");
        let n = cap * 2;
        let mut flats: Vec<Vec<u8>> = Vec::with_capacity(n);
        for i in 0..n {
            let flat = format!("leaf{i:02}").into_bytes();
            let full = prefix(&[flat.as_slice()]);
            auth.register("n0", &full, vec![meta("m", &flat)]).unwrap();
            auth.report_ref(&delta("m", &flat, 1)).unwrap();
            auth.report_ref(&delta("m", &flat, -1)).unwrap();
            flats.push(flat);
            assert!(
                auth.inactive_len("m", "", PoolKind::Target as i32) <= cap,
                "inactive must stay ≤ cap after insert #{i}"
            );
        }
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), cap);
        let (_, early_hit, _) = auth.lookup_prefix(
            "m",
            "",
            PoolKind::Target as i32,
            &prefix(&[flats[0].as_slice()]),
            "n0",
        );
        assert_eq!(early_hit, 1, "skip-insert must not drop view entries");
        let removed = auth.evict_n("m", "", PoolKind::Target as i32, cap);
        assert_eq!(removed, cap, "allocate must clear all inactive at cap");
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 0);
    }

    #[test]
    fn report_refs_mid_batch_must_not_panic_or_drop_peer() {
        let mut auth = Authority::with_inactive_cap(1);
        ensure_model(&mut auth, "m");
        let held = prefix(&[b"held"]);
        let cand = prefix(&[b"cand"]);
        auth.register("n0", &held, vec![meta("m", b"held")])
            .unwrap();
        auth.register("n0", &cand, vec![meta("m", b"cand")])
            .unwrap();

        auth.report_ref(&delta("m", b"cand", 1)).unwrap();
        auth.report_ref(&delta("m", b"cand", -1)).unwrap();
        auth.report_ref(&delta("m", b"held", 1)).unwrap();
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 1);
        assert_eq!(
            auth.global_ref("m", "", PoolKind::Target as i32, b"held"),
            1
        );
        assert_eq!(
            auth.global_ref("m", "", PoolKind::Target as i32, b"cand"),
            0
        );

        auth.report_refs(&[delta("m", b"held", -1), delta("m", b"cand", 1)])
            .unwrap();
        assert_eq!(
            auth.global_ref("m", "", PoolKind::Target as i32, b"held"),
            0
        );
        assert_eq!(
            auth.global_ref("m", "", PoolKind::Target as i32, b"cand"),
            1
        );
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 0);
        let (_, cand_hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &cand, "n0");
        assert_eq!(cand_hit, 1, "peer must not be pressure-evicted mid-batch");
    }

    #[test]
    fn writeback_ref_blocks_evict_until_cleared() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"wb0"]);
        auth.register("n0", &full, vec![meta("m", b"wb0")]).unwrap();
        let mut plus = delta("m", b"wb0", 1);
        plus.kind = RefKind::Writeback as i32;
        auth.report_ref(&plus).unwrap();
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"wb0"), 1);
        assert_eq!(
            auth.evict_n("m", "", PoolKind::Target as i32, 10),
            0,
            "writeback must freeze"
        );

        let mut minus = plus.clone();
        minus.delta = -1;
        auth.report_ref(&minus).unwrap();
        auth.complete_barrier("req-wb", "n0").unwrap();
        assert!(auth.barrier_completed("req-wb"));
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 1), 1);
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 0);
    }

    #[test]
    fn request_barrier_requires_ids() {
        let mut auth = Authority::default();
        assert!(auth.complete_barrier("", "n0").is_err());
        assert!(auth.complete_barrier("r", "").is_err());
    }

    /// S4(issue #74):RefKind 分账——WRITEBACK/REQUEST 分别入账、按 kind 回冲,
    /// 冻结/驱逐语义仍看总数。
    #[test]
    fn s4_ref_accounts_split_by_kind() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"k"]);
        auth.register("n0", &full, vec![meta("m", b"k")]).unwrap();
        let pk = PoolKind::Target as i32;

        let req = delta("m", b"k", 1); // REQUEST+1
        let mut wb = delta("m", b"k", 1);
        wb.kind = RefKind::Writeback as i32; // WRITEBACK+1
        auth.report_ref(&req).unwrap();
        auth.report_ref(&wb).unwrap();
        assert_eq!(auth.global_ref_by_kind("m", "", pk, b"k"), (1, 1));
        assert_eq!(auth.global_ref("m", "", pk, b"k"), 2);

        // WRITEBACK-1 只减 writeback 账簿,request 不动。
        let mut wb_minus = wb.clone();
        wb_minus.delta = -1;
        auth.report_ref(&wb_minus).unwrap();
        assert_eq!(auth.global_ref_by_kind("m", "", pk, b"k"), (1, 0));
        assert_eq!(
            auth.inactive_len("m", "", pk),
            0,
            "总数仍 >0 → 冻结语义不变"
        );

        // REQUEST-1 → 总数归零 → 回到 inactive(驱逐候选)。
        let mut req_minus = req.clone();
        req_minus.delta = -1;
        auth.report_ref(&req_minus).unwrap();
        assert_eq!(auth.global_ref_by_kind("m", "", pk, b"k"), (0, 0));
        assert_eq!(auth.inactive_len("m", "", pk), 1, "总数归零仍标驱逐候选");
    }

    /// S4:kind 级 underflow 报错并指明 kind;IN_FLIGHT/UNSPECIFIED 显式拒绝;
    /// 批内非法 kind 全有或全无。
    #[test]
    fn s4_ref_underflow_scoped_by_kind() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"u"]);
        auth.register("n0", &full, vec![meta("m", b"u")]).unwrap();
        let pk = PoolKind::Target as i32;

        // writeback 账簿有 1;REQUEST-1 必须 kind 级 underflow(总数 1 不抵)。
        let mut wb = delta("m", b"u", 1);
        wb.kind = RefKind::Writeback as i32;
        auth.report_ref(&wb).unwrap();
        let mut req_minus = delta("m", b"u", -1);
        req_minus.node_id = String::new(); // 置空 node 聚焦全局账簿(node 账簿先行校验)
        let err = auth.report_ref(&req_minus).unwrap_err();
        assert!(err.contains("ref_count underflow"), "{err}");
        assert!(err.contains("REQUEST"), "{err}");
        assert_eq!(
            auth.global_ref_by_kind("m", "", pk, b"u"),
            (0, 1),
            "underflow 拒绝后不落账"
        );

        // IN_FLIGHT 预留未接线 → 显式报错(不静默入账)。
        let mut inflight = delta("m", b"u", 1);
        inflight.kind = RefKind::InFlight as i32;
        let err = auth.report_ref(&inflight).unwrap_err();
        assert!(err.contains("unsupported ref_kind"), "{err}");

        // 批内混非法 kind → 整批拒绝,前缀 delta 不落账。
        let err = auth
            .report_refs(&[delta("m", b"u", 1), inflight])
            .unwrap_err();
        assert!(err.contains("unsupported ref_kind"), "{err}");
        assert_eq!(auth.global_ref_by_kind("m", "", pk, b"u"), (0, 1));
    }

    /// S4:死节点 reconcile 按 kind 分别回冲(清账语义不变——总数归零可驱逐)。
    #[test]
    fn s4_dead_node_reconcile_clears_each_kind() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"z"]);
        auth.register("n0", &full, vec![meta("m", b"z")]).unwrap();
        let pk = PoolKind::Target as i32;
        let req = delta("m", b"z", 1); // REQUEST+1 @n0
        let mut wb = delta("m", b"z", 1);
        wb.kind = RefKind::Writeback as i32; // WRITEBACK+1 @n0
        auth.report_ref(&req).unwrap();
        auth.report_ref(&wb).unwrap();
        assert_eq!(auth.global_ref_by_kind("m", "", pk, b"z"), (1, 1));
        assert_eq!(auth.evict_n("m", "", pk, 1), 0, "任一 kind 持有即冻结");

        let (cleared, _) = auth.reconcile_dead_node("n0").unwrap();
        assert_eq!(cleared, 1);
        assert_eq!(auth.global_ref_by_kind("m", "", pk, b"z"), (0, 0));
        assert_eq!(auth.evict_n("m", "", pk, 1), 1, "两本账清零后可驱逐");
    }

    #[test]
    fn report_ref_underflow_is_rejected() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"under"]);
        auth.register("n0", &full, vec![meta("m", b"under")])
            .unwrap();
        let minus = delta("m", b"under", -1);
        let err = auth.report_ref(&minus).unwrap_err();
        assert!(err.contains("underflow"));
        assert_eq!(
            auth.global_ref("m", "", PoolKind::Target as i32, b"under"),
            0
        );
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 0);
    }

    #[test]
    fn report_refs_underflow_is_all_or_nothing() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"ok", b"bad"]);
        auth.register("n0", &full, vec![meta("m", b"ok"), meta("m", b"bad")])
            .unwrap();
        let err = auth
            .report_refs(&[delta("m", b"ok", 1), delta("m", b"bad", -1)])
            .unwrap_err();
        assert!(err.contains("underflow"));
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"ok"), 0);
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"bad"), 0);
    }

    #[test]
    fn report_refs_node_underflow_is_all_or_nothing() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"a", b"b"]);
        auth.register("n0", &full, vec![meta("m", b"a"), meta("m", b"b")])
            .unwrap();
        auth.report_ref(&delta("m", b"a", 1)).unwrap();

        let mut wrong_node_release = delta("m", b"a", -1);
        wrong_node_release.node_id = "n1".into();
        let err = auth
            .report_refs(&[delta("m", b"b", 1), wrong_node_release])
            .unwrap_err();
        assert!(err.contains("node_ref underflow"));
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"a"), 1);
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"b"), 0);
    }

    #[test]
    fn inactive_duplicate_insert_does_not_panic() {
        use kvbm_logical::{FrequencyTrackingCapacity, InactiveIndex, LineageBackend};
        let tracker = FrequencyTrackingCapacity::Small.create_tracker();
        let mut inactive =
            LineageBackend::with_frequency(4, [3, 8, 15], std::sync::Arc::clone(&tracker) as _)
                .unwrap();
        let seq = super::hash_chain::lineage_from_prefix(&prefix(&[b"dup"]))[0];
        inactive.insert(seq, 1);
        inactive.insert(seq, 1);
        assert!(inactive.has(seq));
    }

    #[tokio::test]
    async fn poisoned_authority_lock_returns_status() {
        let cp = ControlPlane::default();
        let inner = cp.inner.clone();
        // RwLock poisons only when a *writer* panics while holding the lock.
        let _ = std::panic::catch_unwind(move || {
            let _guard = inner.write().unwrap();
            panic!("poison authority");
        });
        let err = cp
            .locate(Request::new(LocateRequest { ids: Vec::new() }))
            .await
            .unwrap_err();
        assert_eq!(err.code(), tonic::Code::Internal);
        assert!(cp.authority_poisoned_reported());
    }

    /// P6.1 判据 1: `lookup_prefix` is `&self` — callable through a shared
    /// reference (compile-time proof; was `&mut self` before the lazy-repair
    /// removal). Frequency touch stays on the read path via the internally
    /// synchronized registry.
    #[test]
    fn lookup_prefix_via_shared_ref() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"h0", b"h1"]);
        auth.register("n0", &full, vec![meta("m", b"h0"), meta("m", b"h1")])
            .unwrap();
        let shared: &Authority = &auth;
        let (blocks, hit, _) = shared.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 2);
        assert_eq!(blocks.len(), 2);
    }

    /// P6.1 判据 2: read paths share the RwLock — a lookup RPC is served
    /// while another read guard is held. Under the old `Mutex<Authority>`
    /// this deadlocked (non-reentrant), so the test doubles as a revert
    /// guard (it would hang the harness on revert).
    #[tokio::test]
    #[allow(clippy::await_holding_lock)] // 故意持读 guard 跨 await:验证读-读共享
    async fn lookup_prefix_served_while_read_guard_held() {
        let cp = ControlPlane::default();
        {
            let mut auth = cp.write_authority().unwrap();
            ensure_model(&mut auth, "m");
            let full = prefix(&[b"h0"]);
            auth.register("n0", &full, vec![meta("m", b"h0")]).unwrap();
        }
        let _outer = cp.read_authority().unwrap();
        let resp = cp
            .lookup_prefix(Request::new(LookupPrefixRequest {
                model_id: "m".into(),
                revision: String::new(),
                pool_kind: PoolKind::Target as i32,
                prefix_hashes: prefix(&[b"h0"]),
                requester_node_id: "n0".into(),
            }))
            .await;
        let ok = resp.expect("read path must be served under a concurrent reader");
        assert_eq!(ok.into_inner().hit_length, 1);
    }

    // ---- P6.2: SubscribeView ----

    async fn recv_update(stream: &mut ControlPlaneSubscribeStream) -> ViewUpdate {
        tokio::time::timeout(std::time::Duration::from_secs(2), stream.next())
            .await
            .expect("view update timed out")
            .expect("stream closed early")
            .expect("view update error")
    }

    type ControlPlaneSubscribeStream =
        Pin<Box<dyn tokio_stream::Stream<Item = Result<ViewUpdate, Status>> + Send>>;

    /// 冷启动:snapshot-on-connect(seq=0 全量)+ 锚点(空事件带权威 seq)。
    #[tokio::test]
    async fn subscribe_view_snapshot_on_connect() {
        let cp = ControlPlane::default();
        {
            let mut auth = cp.write_authority().unwrap();
            ensure_model(&mut auth, "m");
            let full = prefix(&[b"h0", b"h1"]);
            auth.register("n0", &full, vec![meta("m", b"h0"), meta("m", b"h1")])
                .unwrap();
            auth.commit_view_events(); // 直接 Authority 调用需手动提交(handler 路径自动)
        }
        let mut stream = cp
            .subscribe_view(Request::new(SubscribeRequest {
                subscriber_id: "r0".into(),
                resume_from_seq: 0,
            }))
            .await
            .unwrap()
            .into_inner();

        let snap = recv_update(&mut stream).await;
        assert_eq!(snap.seq, 0, "snapshot uses reserved seq=0");
        assert_eq!(snap.events.len(), 2);
        assert!(snap
            .events
            .iter()
            .all(|e| e.kind == view_event::Kind::Registered as i32));
        let anchor = recv_update(&mut stream).await;
        assert_eq!(anchor.seq, 1, "anchor carries authoritative last seq");
        assert!(anchor.events.is_empty());
    }

    /// 订阅后的写经广播直播:register RPC → seq=1 REGISTERED 增量。
    #[tokio::test]
    async fn subscribe_view_live_increment_after_register() {
        let cp = ControlPlane::default();
        {
            let mut auth = cp.write_authority().unwrap();
            ensure_model(&mut auth, "m");
        }
        let mut stream = cp
            .subscribe_view(Request::new(SubscribeRequest {
                subscriber_id: "r0".into(),
                resume_from_seq: 0,
            }))
            .await
            .unwrap()
            .into_inner();
        let _snap = recv_update(&mut stream).await; // 空快照
                                                    // last==0 时不发零锚点(anchor_update(0)=None):下一个更新只能是注册后的直播增量。
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(200), stream.next())
                .await
                .is_err(),
            "last==0 时不应再发 seq=0 空锚点"
        );

        let ack = cp
            .register_blocks(Request::new(RegisterBlocksRequest {
                node_id: "n0".into(),
                prefix_hashes: prefix(&[b"h0"]),
                blocks: vec![meta("m", b"h0")],
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(ack.ok, "register_blocks failed: {}", ack.err);

        let live = recv_update(&mut stream).await;
        assert_eq!(live.seq, 1);
        assert_eq!(live.events.len(), 1);
        assert_eq!(live.events[0].kind, view_event::Kind::Registered as i32);
    }

    /// 回归(评审发现):全新 CP 进程 restore 非空 checkpoint 后 view_last_seq()==0,
    /// subscribe 应只收到一个非空快照,不得再发 seq=0 空锚点——接收方把 seq==0
    /// 一律按快照重置,零锚点会把刚应用的快照清空。
    #[tokio::test]
    async fn subscribe_view_after_restore_sends_no_zero_anchor() {
        // donor:造出含两个块的非空快照。
        let donor = ControlPlane::default();
        let snap = {
            let mut auth = donor.write_authority().unwrap();
            ensure_model(&mut auth, "m");
            let full = prefix(&[b"h0", b"h1"]);
            auth.register("n0", &full, vec![meta("m", b"h0"), meta("m", b"h1")])
                .unwrap();
            auth.commit_view_events();
            auth.export_snapshot(1)
        };
        // 全新 CP(模拟重启):经 restore RPC 注入快照。
        let cp = ControlPlane::default();
        let ack = cp
            .restore_checkpoint(Request::new(RestoreCheckpointRequest {
                snapshot: Some(snap),
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(ack.ok, "restore_checkpoint failed: {}", ack.err);
        assert_eq!(
            cp.read_authority().unwrap().view_last_seq(),
            0,
            "全新进程 restore 后未发任何增量事件"
        );

        let mut stream = cp
            .subscribe_view(Request::new(SubscribeRequest {
                subscriber_id: "r0".into(),
                resume_from_seq: 0,
            }))
            .await
            .unwrap()
            .into_inner();
        let snap = recv_update(&mut stream).await;
        assert_eq!(snap.seq, 0, "snapshot uses reserved seq=0");
        assert_eq!(snap.events.len(), 2, "恢复快照应含全部块");
        // 关键断言:last==0 时不再有第二个更新(零锚点)。
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(200), stream.next())
                .await
                .is_err(),
            "last==0 时不应再发 seq=0 空锚点(会清空刚应用的非空快照)"
        );
    }

    /// 回归(复审发现):在线订阅者必须收到 restore 广播的重置快照——此前广播循环的
    /// dedup `seq <= last` 把 seq=0 快照当订阅期重叠丢弃,在线镜像保留旧状态,
    /// 后续新世代增量打在陈旧底座上,永久发散。
    #[tokio::test]
    async fn subscribe_view_live_receives_restore_snapshot() {
        let cp = ControlPlane::default();
        {
            let mut auth = cp.write_authority().unwrap();
            ensure_model(&mut auth, "m");
            auth.register("n0", &prefix(&[b"old"]), vec![meta("m", b"old")])
                .unwrap();
            auth.commit_view_events();
        }
        let mut stream = cp
            .subscribe_view(Request::new(SubscribeRequest {
                subscriber_id: "r0".into(),
                resume_from_seq: 0,
            }))
            .await
            .unwrap()
            .into_inner();
        let snap = recv_update(&mut stream).await;
        assert_eq!(snap.seq, 0);
        assert_eq!(snap.events.len(), 1);
        let anchor = recv_update(&mut stream).await;
        assert_eq!(anchor.seq, 1);

        // 另一权威造新快照(只含 new 块),经 restore 注入并广播给在线订阅者。
        let donor = ControlPlane::default();
        let new_snap = {
            let mut auth = donor.write_authority().unwrap();
            ensure_model(&mut auth, "m");
            auth.register("n0", &prefix(&[b"new"]), vec![meta("m", b"new")])
                .unwrap();
            auth.commit_view_events();
            auth.export_snapshot(1)
        };
        let ack = cp
            .restore_checkpoint(Request::new(RestoreCheckpointRequest {
                snapshot: Some(new_snap),
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(ack.ok, "restore failed: {}", ack.err);

        // 在线流必须收到重置快照(seq=0,内容为恢复后的 new 块)+ 后续锚点。
        let reset = recv_update(&mut stream).await;
        assert_eq!(reset.seq, 0, "restore 快照必须转发给在线订阅者");
        assert_eq!(reset.events.len(), 1);
        assert_eq!(
            reset.events[0].id.as_ref().unwrap().block_hash,
            b"new".to_vec(),
            "快照内容应为恢复后的权威,而非订阅时的旧快照"
        );
        // restore 后 last 保持单调(=1>0)→ 有锚点推进水位。
        let anchor = recv_update(&mut stream).await;
        assert!(anchor.seq >= 1 && anchor.events.is_empty());
    }

    /// 断线 resume:重放 `(N, ...]` 缓冲批,不发快照。
    #[tokio::test]
    async fn subscribe_view_resume_replays_missed() {
        let cp = ControlPlane::default();
        let anchor;
        {
            let mut auth = cp.write_authority().unwrap();
            ensure_model(&mut auth, "m");
            let full = prefix(&[b"h0"]);
            auth.register("n0", &full, vec![meta("m", b"h0")]).unwrap();
            auth.commit_view_events();
            anchor = auth.view_last_seq(); // = 1
                                           // 断线期间的变更:publish L0(seq=2)+ discard(seq=3)
            auth.publish_location(
                "m",
                "",
                PoolKind::Target as i32,
                b"h0",
                Tier::L0,
                "n0",
                true,
            )
            .unwrap();
            auth.commit_view_events();
            auth.discard_blocks(&[KvBlockId {
                model_id: "m".into(),
                revision: String::new(),
                pool_kind: PoolKind::Target as i32,
                block_hash: b"h0".to_vec(),
                scope: "public".into(),
            }])
            .unwrap();
            auth.commit_view_events();
        }
        let mut stream = cp
            .subscribe_view(Request::new(SubscribeRequest {
                subscriber_id: "r0".into(),
                resume_from_seq: anchor,
            }))
            .await
            .unwrap()
            .into_inner();
        let r1 = recv_update(&mut stream).await;
        assert_eq!(r1.seq, 2, "replay starts right after resume point");
        assert_eq!(r1.events[0].kind, view_event::Kind::Moved as i32);
        assert_eq!(r1.events[0].locations.len(), 2, "MOVED 带变更后全量位置");
        let r2 = recv_update(&mut stream).await;
        assert_eq!(r2.seq, 3);
        assert_eq!(r2.events[0].kind, view_event::Kind::Invalidated as i32);
    }

    /// replay buffer 有界:resume 点过老 → 回退 seq=0 快照(判据单测)。
    #[tokio::test]
    async fn subscribe_view_resume_too_old_falls_back_to_snapshot() {
        let cp = ControlPlane::default();
        {
            let mut auth = cp.write_authority().unwrap();
            auth.view_log = crate::view::ViewLog::with_cap(2);
            ensure_model(&mut auth, "m");
            // 4 批提交 → seq 1..=4,buffer 只留 [3,4],floor=3
            for i in 0..4u8 {
                let h = vec![b'a' + i];
                auth.register("n0", &prefix(&[h.as_slice()]), vec![meta("m", &h)])
                    .unwrap();
                auth.commit_view_events();
            }
        }
        let mut stream = cp
            .subscribe_view(Request::new(SubscribeRequest {
                subscriber_id: "r0".into(),
                resume_from_seq: 1, // 1+1=2 < floor=3 → 回退快照
            }))
            .await
            .unwrap()
            .into_inner();
        let first = recv_update(&mut stream).await;
        assert_eq!(first.seq, 0, "过老 resume 回退快照");
        assert_eq!(first.events.len(), 4, "快照携带全量现存 block");
        let anchor = recv_update(&mut stream).await;
        assert_eq!(anchor.seq, 4);
    }

    /// S2(issue #74):resume 点超过已发序号上界(CP 重启场景)→ 回退快照,
    /// 而非空重放(空重放会让客户端带着旧镜像静默发散,见 Go mirror D1)。
    #[tokio::test]
    async fn subscribe_view_resume_ahead_of_issued_seq_falls_back_to_snapshot() {
        let cp = ControlPlane::default();
        {
            let mut auth = cp.write_authority().unwrap();
            ensure_model(&mut auth, "m");
            let full = prefix(&[b"h0"]);
            auth.register("n0", &full, vec![meta("m", b"h0")]).unwrap();
            auth.commit_view_events(); // seq=1,已发上界 last=1
        }
        let mut stream = cp
            .subscribe_view(Request::new(SubscribeRequest {
                subscriber_id: "r0".into(),
                resume_from_seq: 100, // 旧 CP 世代的序号,本实例从未签发
            }))
            .await
            .unwrap()
            .into_inner();
        let first = recv_update(&mut stream).await;
        assert_eq!(first.seq, 0, "超前 resume 必须回退快照");
        assert_eq!(first.events.len(), 1, "快照携带现存 block");
        let anchor = recv_update(&mut stream).await;
        assert_eq!(anchor.seq, 1, "锚点同步本实例权威 seq");
    }

    /// 变更路径事件覆盖:register→REGISTERED / publish→MOVED(全量位置) /
    /// discard→INVALIDATED / 驱逐→INVALIDATED。
    #[test]
    fn view_events_cover_mutation_paths() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"h0", b"h1"]);
        auth.register("n0", &full, vec![meta("m", b"h0"), meta("m", b"h1")])
            .unwrap();
        let u = auth.commit_view_events().unwrap();
        assert_eq!(u.seq, 1);
        assert_eq!(u.events.len(), 2);
        assert!(u
            .events
            .iter()
            .all(|e| e.kind == view_event::Kind::Registered as i32));

        auth.publish_location(
            "m",
            "",
            PoolKind::Target as i32,
            b"h0",
            Tier::L0,
            "n0",
            true,
        )
        .unwrap();
        let u = auth.commit_view_events().unwrap();
        assert_eq!(u.events.len(), 1);
        assert_eq!(u.events[0].kind, view_event::Kind::Moved as i32);
        assert_eq!(u.events[0].locations.len(), 2, "MOVED 带变更后全量位置");

        auth.discard_blocks(&[KvBlockId {
            model_id: "m".into(),
            revision: String::new(),
            pool_kind: PoolKind::Target as i32,
            block_hash: b"h0".to_vec(),
            scope: "public".into(),
        }])
        .unwrap();
        let u = auth.commit_view_events().unwrap();
        assert_eq!(u.events.len(), 1);
        assert_eq!(u.events[0].kind, view_event::Kind::Invalidated as i32);

        // 驱逐路径:report_ref 0→>0→0 入 inactive 后 evict → INVALIDATED
        auth.report_refs(&[delta("m", b"h1", 1)]).unwrap();
        auth.report_refs(&[delta("m", b"h1", -1)]).unwrap();
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 1), 1);
        let u = auth.commit_view_events().unwrap();
        assert!(u
            .events
            .iter()
            .any(|e| e.kind == view_event::Kind::Invalidated as i32));
    }

    /// relocate_in_view(defrag Moved)同样发 MOVED 事件(P6.2 覆盖补齐,review #54)。
    #[test]
    fn view_events_cover_relocate_in_view() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        auth.register("n0", &prefix(&[b"h0"]), vec![meta("m", b"h0")])
            .unwrap();
        auth.commit_view_events();
        auth.relocate_in_view(
            "m",
            "",
            PoolKind::Target as i32,
            b"h0",
            Tier::L2,
            "n0",
            7,
            42,
        )
        .unwrap();
        let u = auth.commit_view_events().unwrap();
        assert_eq!(u.events.len(), 1);
        assert_eq!(u.events[0].kind, view_event::Kind::Moved as i32);
        assert_eq!(u.events[0].locations[0].segment_id, 7);
        assert_eq!(u.events[0].locations[0].offset, 42);
    }

    #[test]
    fn register_rejects_invented_l2() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"bare"]);
        let mut m = meta("m", b"bare");
        m.locations.clear();
        assert!(auth.register("n0", &full, vec![m]).is_err());
    }

    #[test]
    fn publish_l0_enables_local_hit() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"loc"]);
        auth.register("n0", &full, vec![meta("m", b"loc")]).unwrap();
        auth.publish_location(
            "m",
            "",
            PoolKind::Target as i32,
            b"loc",
            Tier::L0,
            "n0",
            true,
        )
        .unwrap();
        assert!(auth.has_l0_on("m", "", PoolKind::Target as i32, b"loc", "n0"));
        let (_, _, all_local) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert!(all_local);
        auth.publish_location(
            "m",
            "",
            PoolKind::Target as i32,
            b"loc",
            Tier::L0,
            "n0",
            false,
        )
        .unwrap();
        let (_, _, all_local) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert!(!all_local);
    }

    /// P4.5: two models, same prefix hash → no cross-namespace hit.
    #[test]
    fn p45_two_models_same_hash_no_crosstalk() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "llama");
        ensure_model(&mut auth, "qwen");
        let full = prefix(&[b"samehash"]);
        auth.register("n0", &full, vec![meta("llama", b"samehash")])
            .unwrap();
        // qwen has no blocks yet
        let (_, hit_q, _) = auth.lookup_prefix("qwen", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit_q, 0);
        auth.register("n0", &full, vec![meta("qwen", b"samehash")])
            .unwrap();
        let (_, hit_l, _) = auth.lookup_prefix("llama", "", PoolKind::Target as i32, &full, "n0");
        let (_, hit_q, _) = auth.lookup_prefix("qwen", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit_l, 1);
        assert_eq!(hit_q, 1);
        auth.deregister_model("llama", "").unwrap();
        let (_, hit_l, _) = auth.lookup_prefix("llama", "", PoolKind::Target as i32, &full, "n0");
        let (_, hit_q, _) = auth.lookup_prefix("qwen", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit_l, 0, "llama cascaded away");
        assert_eq!(hit_q, 1, "qwen untouched");
    }

    /// P4.5: DeregisterModel clears radix/locations for that namespace.
    #[test]
    fn p45_deregister_cascades_view() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"a", b"b"]);
        auth.register("n0", &full, vec![meta("m", b"a"), meta("m", b"b")])
            .unwrap();
        assert_eq!(auth.block_count("m", ""), 2);
        auth.deregister_model("m", "").unwrap();
        assert!(!auth.has_namespace("m", ""));
        assert_eq!(auth.block_count("m", ""), 0);
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 0);
        // re-register model + blocks works on a fresh namespace
        ensure_model(&mut auth, "m");
        auth.register("n0", &full, vec![meta("m", b"a")]).unwrap();
        let (_, hit, _) =
            auth.lookup_prefix("m", "", PoolKind::Target as i32, &prefix(&[b"a"]), "n0");
        assert_eq!(hit, 1);
    }

    /// P4.5: new revision = new namespace; old revision remains until deregistered.
    #[test]
    fn p45_revision_isolation_and_invalidate() {
        let mut auth = Authority::default();
        ensure_model_rev(&mut auth, "m", "r1");
        ensure_model_rev(&mut auth, "m", "r2");
        let full = prefix(&[b"pfx"]);
        auth.register("n0", &full, vec![meta_rev("m", "r1", b"pfx")])
            .unwrap();
        auth.register("n0", &full, vec![meta_rev("m", "r2", b"pfx")])
            .unwrap();
        let (_, h1, _) = auth.lookup_prefix("m", "r1", PoolKind::Target as i32, &full, "n0");
        let (_, h2, _) = auth.lookup_prefix("m", "r2", PoolKind::Target as i32, &full, "n0");
        assert_eq!(h1, 1);
        assert_eq!(h2, 1);
        // miss across revision
        let (_, h_cross, _) = auth.lookup_prefix("m", "r1", PoolKind::Target as i32, &full, "n0");
        assert_eq!(h_cross, 1);
        auth.deregister_model("m", "r1").unwrap();
        let (_, h1, _) = auth.lookup_prefix("m", "r1", PoolKind::Target as i32, &full, "n0");
        let (_, h2, _) = auth.lookup_prefix("m", "r2", PoolKind::Target as i32, &full, "n0");
        assert_eq!(h1, 0, "r1 invalidated");
        assert_eq!(h2, 1, "r2 still live");
        assert!(auth.has_namespace("m", "r2"));
        assert!(!auth.has_namespace("m", "r1"));
    }

    #[test]
    fn register_model_idempotent_keeps_blocks() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"keep"]);
        auth.register("n0", &full, vec![meta("m", b"keep")])
            .unwrap();
        // Same identity + quota bump is ok.
        auth.register_model(ModelDescriptor {
            model_id: "m".into(),
            revision: String::new(),
            num_layers: 32,
            block_spec: Some(BlockSpec {
                block_tokens: 128,
                bytes_per_block: 0,
            }),
            hash_algo: HashAlgo::HashSha256256 as i32,
            quota: Some(Quota {
                soft_bytes: 1,
                hard_bytes: 2,
                borrow_enabled: true,
            }),
        })
        .unwrap();
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 1);
        let q = auth
            .model_descriptor("m", "")
            .and_then(|d| d.quota)
            .unwrap();
        assert_eq!(q.soft_bytes, 1);
        assert_eq!(q.hard_bytes, 2);
        assert!(q.borrow_enabled);
    }

    #[test]
    fn register_model_rejects_immutable_change() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let err = auth
            .register_model(ModelDescriptor {
                model_id: "m".into(),
                revision: String::new(),
                num_layers: 64, // changed
                block_spec: Some(BlockSpec {
                    block_tokens: 128,
                    bytes_per_block: 0,
                }),
                hash_algo: HashAlgo::HashSha256256 as i32,
                quota: None,
            })
            .unwrap_err();
        assert!(err.contains("immutable") || err.contains("new revision"));
        // blocks still addressable under original descriptor
        let full = prefix(&[b"x"]);
        auth.register("n0", &full, vec![meta("m", b"x")]).unwrap();
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 1);
    }

    /// Same (model, revision, hash) under TARGET vs DRAFT must not crosstalk.
    #[test]
    fn p45_target_draft_same_hash_no_crosstalk() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"shared"]);
        auth.register("n0", &full, vec![meta("m", b"shared")])
            .unwrap();
        let mut draft = meta("m", b"shared");
        draft.id.as_mut().unwrap().pool_kind = PoolKind::Draft as i32;
        auth.register("n0", &full, vec![draft]).unwrap();

        let (_, t_hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        let (_, d_hit, _) = auth.lookup_prefix("m", "", PoolKind::Draft as i32, &full, "n0");
        assert_eq!(t_hit, 1);
        assert_eq!(d_hit, 1);

        // Locate by pool_kind
        let t_id = KvBlockId {
            model_id: "m".into(),
            block_hash: b"shared".to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
            revision: String::new(),
        };
        let mut d_id = t_id.clone();
        d_id.pool_kind = PoolKind::Draft as i32;
        assert_eq!(auth.locate(std::slice::from_ref(&t_id)).len(), 1);
        assert_eq!(auth.locate(std::slice::from_ref(&d_id)).len(), 1);

        // Evict TARGET only
        auth.report_ref(&delta("m", b"shared", 1)).unwrap();
        auth.report_ref(&delta("m", b"shared", -1)).unwrap();
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 1), 1);
        let (_, t_hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        let (_, d_hit, _) = auth.lookup_prefix("m", "", PoolKind::Draft as i32, &full, "n0");
        assert_eq!(t_hit, 0, "target evicted");
        assert_eq!(d_hit, 1, "draft untouched");
    }

    // --- P4.6: soft/hard quota + borrow + backpressure ---

    fn ensure_model_quota(
        auth: &mut Authority,
        model: &str,
        soft: i64,
        hard: i64,
        borrow: bool,
        bpb: u64,
    ) {
        auth.register_model(ModelDescriptor {
            model_id: model.into(),
            revision: String::new(),
            num_layers: 32,
            block_spec: Some(BlockSpec {
                block_tokens: 128,
                bytes_per_block: bpb,
            }),
            hash_algo: HashAlgo::HashSha256256 as i32,
            quota: Some(Quota {
                soft_bytes: soft,
                hard_bytes: hard,
                borrow_enabled: borrow,
            }),
        })
        .unwrap();
    }

    /// A hits hard quota → RejectedHardQuota; B unaffected.
    #[test]
    fn p46_hard_quota_a_does_not_affect_b() {
        let mut auth = Authority::default();
        ensure_model_quota(&mut auth, "A", 100, 200, false, 100);
        ensure_model_quota(&mut auth, "B", 100, 200, false, 100);

        // A: 2 blocks = 200 = hard; third rejected
        assert_eq!(
            auth.register("n0", &prefix(&[b"a0"]), vec![meta("A", b"a0")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        assert_eq!(
            auth.register("n0", &prefix(&[b"a1"]), vec![meta("A", b"a1")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        match auth
            .register("n0", &prefix(&[b"a2"]), vec![meta("A", b"a2")])
            .unwrap()
        {
            RegisterStatus::RejectedHardQuota(bp) => {
                assert_eq!(bp.model_id, "A");
                assert_eq!(bp.reason, "HARD_QUOTA");
                assert!(bp.deficit_bytes > 0);
            }
            other => panic!("expected RejectedHardQuota, got {other:?}"),
        }
        assert_eq!(auth.used_bytes("A", ""), 200);
        assert_eq!(auth.block_count("A", ""), 2);

        // B still writes
        assert_eq!(
            auth.register("n0", &prefix(&[b"b0"]), vec![meta("B", b"b0")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        assert_eq!(auth.used_bytes("B", ""), 100);
        let (_, hit, _) =
            auth.lookup_prefix("B", "", PoolKind::Target as i32, &prefix(&[b"b0"]), "n0");
        assert_eq!(hit, 1);
    }

    /// Borrow over soft from pool free; reclaim when another borrower needs space.
    #[test]
    fn p46_borrow_and_reclaim() {
        let mut auth = Authority::default();
        auth.set_pool_capacity_bytes(300);
        // Both may borrow; capacity forces reclaim of A's over-soft bytes for B.
        ensure_model_quota(&mut auth, "A", 100, 300, true, 100);
        ensure_model_quota(&mut auth, "B", 100, 300, true, 100);

        // A: soft + one borrowed block (used=200)
        assert_eq!(
            auth.register("n0", &prefix(&[b"a0"]), vec![meta("A", b"a0")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        assert_eq!(
            auth.register("n0", &prefix(&[b"a1"]), vec![meta("A", b"a1")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        let snap = auth.get_model_quota("A", "").unwrap();
        assert_eq!(snap.used_bytes, 200);
        assert_eq!(snap.borrowed_bytes, 100);

        // Make A's borrowed block inactive (reclaimable)
        auth.report_ref(&delta("A", b"a1", 1)).unwrap();
        auth.report_ref(&delta("A", b"a1", -1)).unwrap();

        // B takes soft (pool free → 0)
        assert_eq!(
            auth.register("n0", &prefix(&[b"b0"]), vec![meta("B", b"b0")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        // B second block needs borrow but free=0 → reclaim A's over-soft
        assert_eq!(
            auth.register("n0", &prefix(&[b"b1"]), vec![meta("B", b"b1")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        assert!(auth.used_bytes("A", "") <= 100, "A borrow reclaimed");
        assert_eq!(auth.used_bytes("B", ""), 200);
        let (_, a_hit, _) =
            auth.lookup_prefix("A", "", PoolKind::Target as i32, &prefix(&[b"a1"]), "n0");
        assert_eq!(a_hit, 0, "reclaimed a1");
    }

    /// Under hard quota but out of shared pool capacity must not masquerade as HARD_QUOTA.
    #[test]
    fn p46_pool_capacity_reject_is_not_hard_quota() {
        let mut auth = Authority::default();
        auth.set_pool_capacity_bytes(200);
        ensure_model_quota(&mut auth, "m", 100, 300, true, 100);

        assert_eq!(
            auth.register("n0", &prefix(&[b"a0"]), vec![meta("m", b"a0")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        assert_eq!(
            auth.register("n0", &prefix(&[b"a1"]), vec![meta("m", b"a1")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        match auth
            .register("n0", &prefix(&[b"a2"]), vec![meta("m", b"a2")])
            .unwrap()
        {
            RegisterStatus::RejectedHardQuota(bp) => {
                assert_eq!(bp.reason, "POOL_CAPACITY");
                assert_eq!(bp.hard_bytes, 300);
                assert!(bp.deficit_bytes > 0);
            }
            other => panic!("expected capacity reject, got {other:?}"),
        }
        assert_eq!(auth.block_count("m", ""), 2);
    }

    /// SetModelQuota / GetModelQuota + hard backpressure on RegisterBlocks.
    #[test]
    fn p46_set_get_quota_and_backpressure_signal() {
        let mut auth = Authority::default();
        ensure_model_quota(&mut auth, "m", 0, 0, false, 50); // unlimited initially
        assert_eq!(
            auth.register("n0", &prefix(&[b"x"]), vec![meta("m", b"x")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        auth.set_model_quota(
            "m",
            "",
            Quota {
                soft_bytes: 50,
                hard_bytes: 50,
                borrow_enabled: false,
            },
        )
        .unwrap();
        let g = auth.get_model_quota("m", "").unwrap();
        assert_eq!(g.used_bytes, 50);
        assert_eq!(g.quota.as_ref().unwrap().hard_bytes, 50);

        match auth
            .register("n0", &prefix(&[b"y"]), vec![meta("m", b"y")])
            .unwrap()
        {
            RegisterStatus::RejectedHardQuota(bp) => {
                assert_eq!(bp.reason, "HARD_QUOTA");
                assert_eq!(bp.hard_bytes, 50);
            }
            other => panic!("expected backpressure reject, got {other:?}"),
        }
    }

    /// Hard reject must not evict existing reusable inactive blocks (review #1).
    #[test]
    fn p46_hard_reject_is_side_effect_free() {
        let mut auth = Authority::default();
        ensure_model_quota(&mut auth, "m", 200, 200, false, 100);
        assert_eq!(
            auth.register("n0", &prefix(&[b"a0"]), vec![meta("m", b"a0")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        assert_eq!(
            auth.register("n0", &prefix(&[b"a1"]), vec![meta("m", b"a1")])
                .unwrap(),
            RegisterStatus::Accepted
        );
        // a1 inactive (reusable); lower hard so even full inactive eviction cannot fit.
        auth.set_model_quota(
            "m",
            "",
            Quota {
                soft_bytes: 100,
                hard_bytes: 100,
                borrow_enabled: false,
            },
        )
        .unwrap();
        auth.report_ref(&delta("m", b"a1", 1)).unwrap();
        auth.report_ref(&delta("m", b"a1", -1)).unwrap();
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 1);

        match auth
            .register("n0", &prefix(&[b"a2"]), vec![meta("m", b"a2")])
            .unwrap()
        {
            RegisterStatus::RejectedHardQuota(_) => {}
            other => panic!("expected RejectedHardQuota, got {other:?}"),
        }
        // a1 must still be lookup-able (not evicted on the reject path).
        let (_, hit, _) =
            auth.lookup_prefix("m", "", PoolKind::Target as i32, &prefix(&[b"a1"]), "n0");
        assert_eq!(hit, 1, "hard reject must not evict existing blocks");
        assert_eq!(auth.block_count("m", ""), 2);
        assert_eq!(auth.used_bytes("m", ""), 200);
    }

    /// RegisterModel shares SetModelQuota validator (review #3).
    #[test]
    fn p46_register_model_rejects_invalid_quota() {
        let mut auth = Authority::default();
        let err = auth
            .register_model(ModelDescriptor {
                model_id: "bad".into(),
                revision: String::new(),
                num_layers: 1,
                block_spec: Some(BlockSpec {
                    block_tokens: 128,
                    bytes_per_block: 1,
                }),
                hash_algo: HashAlgo::HashSha256256 as i32,
                quota: Some(Quota {
                    soft_bytes: 200,
                    hard_bytes: 100,
                    borrow_enabled: false,
                }),
            })
            .unwrap_err();
        assert!(err.contains("soft_bytes") || err.contains("quota"));

        ensure_model_quota(&mut auth, "ok", 100, 200, false, 1);
        let err = auth
            .register_model(ModelDescriptor {
                model_id: "ok".into(),
                revision: String::new(),
                num_layers: 32,
                block_spec: Some(BlockSpec {
                    block_tokens: 128,
                    bytes_per_block: 1,
                }),
                hash_algo: HashAlgo::HashSha256256 as i32,
                quota: Some(Quota {
                    soft_bytes: -1,
                    hard_bytes: 0,
                    borrow_enabled: false,
                }),
            })
            .unwrap_err();
        assert!(err.contains("non-negative") || err.contains("quota"));
    }

    // --- P4.7: GC / orphan / reconcile / checkpoint ---

    fn meta_l0_l2(model: &str, hash: &[u8]) -> BlockMeta {
        let mut m = meta(model, hash);
        m.locations.push(Location {
            tier: Tier::L0 as i32,
            node_id: "n0".into(),
            segment_id: 1,
            offset: 0,
        });
        m
    }

    /// Cold GC strips L0/L1 but keeps radix + L2 (durable backing).
    #[test]
    fn p47_cold_gc_strips_l0_keeps_l2() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"c0"]);
        auth.register("n0", &full, vec![meta_l0_l2("m", b"c0")])
            .unwrap();
        assert!(auth.has_l0_on("m", "", PoolKind::Target as i32, b"c0", "n0"));
        // Make inactive
        auth.report_ref(&delta("m", b"c0", 1)).unwrap();
        auth.report_ref(&delta("m", b"c0", -1)).unwrap();

        let (stripped, discarded) = auth.gc_cold("m", "", PoolKind::Target as i32, 1).unwrap();
        assert_eq!(stripped.len(), 1);
        assert!(discarded.is_empty());
        assert!(!auth.has_l0_on("m", "", PoolKind::Target as i32, b"c0", "n0"));
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 1, "radix + L2 must remain");
    }

    /// Orphan TTL → metadata discard (Mooncake zombie analogue).
    #[test]
    fn p47_orphan_ttl_discard() {
        use std::sync::atomic::{AtomicU64, Ordering};
        static NOW: AtomicU64 = AtomicU64::new(1_000);
        fn fake_now() -> u64 {
            NOW.load(Ordering::SeqCst)
        }

        let mut auth = Authority::default();
        auth.set_now_ms_fn(fake_now);
        ensure_model(&mut auth, "m");
        auth.report_orphans(&[OrphanReport {
            node_id: "n0".into(),
            ids: vec![KvBlockId {
                model_id: "m".into(),
                block_hash: b"zombie".to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
                revision: String::new(),
            }],
            marked_at_ms: 1_000,
        }])
        .unwrap();

        // Not yet expired
        let early = auth.sweep_orphans(30_000);
        assert!(early.is_empty());

        NOW.store(1_000 + 30_000, Ordering::SeqCst);
        let gone = auth.sweep_orphans(30_000);
        assert_eq!(gone.len(), 1);
        assert_eq!(gone[0].block_hash, b"zombie");
    }

    /// Dead-node reconcile clears writeback-held refs (P4.3 leak subset).
    #[test]
    fn p47_dead_node_clears_writeback_ref() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"wb"]);
        auth.register("n0", &full, vec![meta("m", b"wb")]).unwrap();
        // Simulate WRITEBACK+1 then agent crash (no -1).
        auth.report_ref(&RefDelta {
            id: Some(KvBlockId {
                model_id: "m".into(),
                block_hash: b"wb".to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
                revision: String::new(),
            }),
            kind: RefKind::Writeback as i32,
            delta: 1,
            node_id: "n0".into(),
        })
        .unwrap();
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"wb"), 1);
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 1), 0);

        let (cleared, _) = auth.reconcile_dead_node("n0").unwrap();
        assert_eq!(cleared, 1);
        assert_eq!(auth.global_ref("m", "", PoolKind::Target as i32, b"wb"), 0);
        // Now evictable
        assert_eq!(auth.evict_n("m", "", PoolKind::Target as i32, 1), 1);
    }

    /// H4: dead-node reconcile must decrement the L0 presence marker per
    /// dead node, not only when no node holds L0. With a blanket `!any(L0)`
    /// guard the shadow count stays inflated when sibling nodes still hold
    /// L0, so the final dead node leaves count=1 though no L0 remains.
    #[test]
    fn p47_dead_node_l0_presence_precise() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let pk = PoolKind::Target as i32;
        auth.register("n0", &prefix(&[b"l0"]), vec![meta("m", b"l0")])
            .unwrap();
        // Two nodes each hold an L0 replica → presence count = 2.
        auth.publish_location("m", "", pk, b"l0", Tier::L0, "n0", true)
            .unwrap();
        auth.publish_location("m", "", pk, b"l0", Tier::L0, "n1", true)
            .unwrap();
        assert!(auth.has_l0_presence("m", "", pk, b"l0"));
        assert!(auth.has_l0_on("m", "", pk, b"l0", "n0"));
        assert!(auth.has_l0_on("m", "", pk, b"l0", "n1"));

        // Dead n0: count 2→1. n1 still holds L0 → shadow must stay true.
        auth.reconcile_dead_node("n0").unwrap();
        assert!(
            auth.has_l0_presence("m", "", pk, b"l0"),
            "one L0 replica remains on n1"
        );
        assert!(!auth.has_l0_on("m", "", pk, b"l0", "n0"));
        assert!(auth.has_l0_on("m", "", pk, b"l0", "n1"));

        // Dead n1: count 1→0. No L0 left → shadow must read false.
        auth.reconcile_dead_node("n1").unwrap();
        assert!(
            !auth.has_l0_presence("m", "", pk, b"l0"),
            "no L0 replica remains; shadow must be cleared"
        );
        assert!(!auth.has_l0_on("m", "", pk, b"l0", "n1"));
    }

    /// 回归(评审发现):账簿为 i64 而 proto `RefDelta.delta` 为 int32——dead-node
    /// 回冲计数 > i32::MAX 时必须拆批清零,不得 `as i32` 截留(截留会让账清不净,
    /// 且错误被 best-effort 丢弃后无任何信号)。
    #[test]
    fn reconcile_dead_node_splits_oversized_ref_counts() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let pk = PoolKind::Target as i32;
        auth.register("n0", &prefix(&[b"big"]), vec![meta("m", b"big")])
            .unwrap();
        let id = KvBlockId {
            model_id: "m".into(),
            block_hash: b"big".to_vec(),
            pool_kind: pk,
            scope: "public".into(),
            revision: String::new(),
        };
        // 垫账到 i32::MAX+2(模拟计数腐化/失控 agent):单 delta 上限即 i32::MAX,两笔入账。
        for d in [i32::MAX, 2] {
            auth.report_ref(&RefDelta {
                id: Some(id.clone()),
                kind: RefKind::Request as i32,
                delta: d,
                node_id: "n0".into(),
            })
            .unwrap();
        }
        let huge = i32::MAX as i64 + 2;
        assert_eq!(auth.global_ref("m", "", pk, b"big"), huge);

        let (cleared, _) = auth.reconcile_dead_node("n0").unwrap();
        assert_eq!(cleared, 1);
        assert_eq!(
            auth.global_ref("m", "", pk, b"big"),
            0,
            "拆批回冲必须清零,不得截留"
        );
    }

    /// Checkpoint save/restore rebuilds namespace + blocks (metadata before bytes).
    #[test]
    fn p47_checkpoint_roundtrip() {
        let store = MemoryCheckpointStore::new();
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"p"]);
        auth.register("n0", &full, vec![meta("m", b"p")]).unwrap();
        let snap = auth.export_snapshot(1);
        store.save(snap.clone()).unwrap();

        let mut auth2 = Authority::default();
        auth2
            .import_snapshot(&store.load().unwrap().unwrap())
            .unwrap();
        assert!(auth2.has_namespace("m", ""));
        let (_, hit, _) = auth2.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 1);
    }

    /// Multi-block prefix lineage survives checkpoint restore (not flat-root register).
    #[test]
    fn p47_checkpoint_multilineage_restore() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"h0", b"h1"]);
        auth.register("n0", &full, vec![meta("m", b"h0"), meta("m", b"h1")])
            .unwrap();
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 2);

        let snap = auth.export_snapshot(2);
        assert_eq!(snap.blocks.len(), 2);
        assert!(
            snap.blocks.iter().any(|b| b.prefix_chain.len() == 2),
            "child block must export full prefix_chain"
        );

        let mut auth2 = Authority::default();
        auth2.import_snapshot(&snap).unwrap();
        let (_, hit2, _) = auth2.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit2, 2, "restore must keep seq_hash lineage for h0→h1");
    }

    /// Restore must fail loudly if the snapshot's model quota rejects a block.
    #[test]
    fn p47_checkpoint_restore_rejects_quota_loss() {
        let mut auth = Authority::default();
        ensure_model_quota(&mut auth, "m", 0, 0, false, 100);
        auth.register("n0", &prefix(&[b"q"]), vec![meta("m", b"q")])
            .unwrap();
        let mut snap = auth.export_snapshot(3);
        snap.models[0].quota = Some(Quota {
            soft_bytes: 50,
            hard_bytes: 50,
            borrow_enabled: false,
        });

        let mut restored = Authority::default();
        let err = restored.import_snapshot(&snap).unwrap_err();
        assert!(
            err.contains("import_snapshot register rejected"),
            "restore must not report success after dropping a block: {err}"
        );
        assert_eq!(restored.block_count("m", ""), 0);
    }

    /// Durable cold peel re-inserts into inactive for later reclaim accounting.
    #[test]
    fn p47_cold_gc_reinserts_durable_inactive() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"c0"]);
        auth.register("n0", &full, vec![meta_l0_l2("m", b"c0")])
            .unwrap();
        auth.report_ref(&delta("m", b"c0", 1)).unwrap();
        auth.report_ref(&delta("m", b"c0", -1)).unwrap();
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 1);

        let (stripped, discarded) = auth.gc_cold("m", "", PoolKind::Target as i32, 1).unwrap();
        assert_eq!(stripped.len(), 1);
        assert!(discarded.is_empty());
        assert_eq!(
            auth.inactive_len("m", "", PoolKind::Target as i32),
            1,
            "durable victim must return to inactive after L0/L1 peel"
        );
        // Second GC still sees the block (no-op peel; still reclaimable).
        let (stripped2, _) = auth.gc_cold("m", "", PoolKind::Target as i32, 1).unwrap();
        assert_eq!(stripped2.len(), 1);
        assert_eq!(auth.inactive_len("m", "", PoolKind::Target as i32), 1);
    }

    /// Stale orphan mark must not TTL-kill a block that later registered.
    #[test]
    fn p47_orphan_cleared_on_register() {
        use std::sync::atomic::{AtomicU64, Ordering};
        static NOW: AtomicU64 = AtomicU64::new(1_000);
        fn fake_now() -> u64 {
            NOW.load(Ordering::SeqCst)
        }

        let mut auth = Authority::default();
        auth.set_now_ms_fn(fake_now);
        ensure_model(&mut auth, "m");
        let id = KvBlockId {
            model_id: "m".into(),
            block_hash: b"late".to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
            revision: String::new(),
        };
        auth.report_orphans(&[OrphanReport {
            node_id: "n0".into(),
            ids: vec![id.clone()],
            marked_at_ms: 1_000,
        }])
        .unwrap();

        // PutEnd wins before TTL.
        let full = prefix(&[b"late"]);
        auth.register("n0", &full, vec![meta("m", b"late")])
            .unwrap();

        NOW.store(1_000 + 30_000, Ordering::SeqCst);
        let gone = auth.sweep_orphans(30_000);
        assert!(
            gone.is_empty(),
            "registered block must not be discarded by stale orphan TTL"
        );
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 1);
    }

    /// Explicit discard must not remove globally referenced blocks.
    #[test]
    fn p47_discard_rejects_refed_block() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"live"]);
        auth.register("n0", &full, vec![meta("m", b"live")])
            .unwrap();
        auth.report_ref(&delta("m", b"live", 1)).unwrap();

        let id = KvBlockId {
            model_id: "m".into(),
            block_hash: b"live".to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
            revision: String::new(),
        };
        let err = auth.discard_blocks(&[id]).unwrap_err();
        assert!(err.contains("global_refs"));
        let (_, hit, _) = auth.lookup_prefix("m", "", PoolKind::Target as i32, &full, "n0");
        assert_eq!(hit, 1, "refed block must remain visible");
    }

    // --- P4.8: defrag plan + placement ---

    fn meta_l2_at(model: &str, hash: &[u8], seg: u64, off: u64) -> BlockMeta {
        let mut m = meta(model, hash);
        m.locations[0].segment_id = seg;
        m.locations[0].offset = off;
        m
    }

    fn l2_offset(auth: &Authority, model: &str, hash: &[u8]) -> (u64, u64) {
        let id = KvBlockId {
            model_id: model.into(),
            block_hash: hash.to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
            revision: String::new(),
        };
        let metas = auth.locate(&[id]);
        let loc = metas[0]
            .locations
            .iter()
            .find(|l| l.tier == Tier::L2 as i32)
            .expect("L2");
        (loc.segment_id, loc.offset)
    }

    /// Compact plan detects non-dense L2 offsets in a segment.
    #[test]
    fn p48_plan_compact_on_holes() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let slot = 100u64;
        auth.register(
            "n0",
            &prefix(&[b"a", b"b", b"c"]),
            vec![
                meta_l2_at("m", b"a", 1, 0),
                meta_l2_at("m", b"b", 1, 200),
                meta_l2_at("m", b"c", 1, 300),
            ],
        )
        .unwrap();
        let moves = auth
            .plan_defrag("m", "", PoolKind::Target as i32, DefragMode::Compact, slot)
            .unwrap();
        assert_eq!(moves.len(), 1);
        assert!(moves[0].compact_segment);
        assert_eq!(moves[0].segment_id, 1);
    }

    /// Compacting a mixed segment would move ref>0 slots; skip the whole segment.
    #[test]
    fn p48_plan_compact_skips_segment_with_refed_block() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let slot = 100u64;
        auth.register(
            "n0",
            &prefix(&[b"a", b"b", b"c"]),
            vec![
                meta_l2_at("m", b"a", 1, 0),
                meta_l2_at("m", b"b", 1, 200),
                meta_l2_at("m", b"c", 1, 300),
            ],
        )
        .unwrap();
        auth.report_ref(&delta("m", b"b", 1)).unwrap();
        let moves = auth
            .plan_defrag("m", "", PoolKind::Target as i32, DefragMode::Compact, slot)
            .unwrap();
        assert!(
            moves.is_empty(),
            "segment containing ref>0 block must not be compacted: {moves:?}"
        );
    }

    /// Co-locate plan packs a scattered prefix chain onto one segment (same node).
    #[test]
    fn p48_plan_colocate_prefix_chain() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let slot = 64u64;
        auth.register(
            "n0",
            &prefix(&[b"h0", b"h1"]),
            vec![meta_l2_at("m", b"h0", 1, 0), meta_l2_at("m", b"h1", 2, 0)],
        )
        .unwrap();
        let moves = auth
            .plan_defrag("m", "", PoolKind::Target as i32, DefragMode::Colocate, slot)
            .unwrap();
        assert!(
            moves
                .iter()
                .any(|m| !m.compact_segment && m.to_segment == 1),
            "expected co-locate onto seg1; got {moves:?}"
        );
        assert!(moves.iter().all(|m| m.node_id == "n0"));
    }

    /// Cross-node prefix scatter must NOT yield local CoLocateMove (no Transfer yet).
    #[test]
    fn p48_plan_colocate_skips_cross_node() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let slot = 64u64;
        let mut m0 = meta_l2_at("m", b"h0", 1, 0);
        m0.locations[0].node_id = "n0".into();
        let mut m1 = meta_l2_at("m", b"h1", 1, 0);
        m1.locations[0].node_id = "n1".into();
        auth.register("n0", &prefix(&[b"h0", b"h1"]), vec![m0, m1])
            .unwrap();
        let moves = auth
            .plan_defrag("m", "", PoolKind::Target as i32, DefragMode::Colocate, slot)
            .unwrap();
        assert!(
            moves.is_empty(),
            "cross-node co-locate deferred to P5; got {moves:?}"
        );
    }

    /// Co-locate must not target a slot held by an unrelated block (arena would fail).
    #[test]
    fn p48_plan_colocate_avoids_occupied_slots() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let slot = 64u64;
        // Foreign block owns seg1/off0 — naive pack onto members[0].seg at base 0 would collide.
        auth.register("n0", &prefix(&[b"x"]), vec![meta_l2_at("m", b"x", 1, 0)])
            .unwrap();
        auth.register(
            "n0",
            &prefix(&[b"h0", b"h1"]),
            vec![
                meta_l2_at("m", b"h0", 1, 128), // not dense at 0
                meta_l2_at("m", b"h1", 2, 0),
            ],
        )
        .unwrap();
        let moves = auth
            .plan_defrag("m", "", PoolKind::Target as i32, DefragMode::Colocate, slot)
            .unwrap();
        assert!(
            !moves.is_empty(),
            "should still find a free pack target; got empty"
        );
        for m in &moves {
            assert!(
                !(m.to_segment == 1 && m.to_offset == 0),
                "must not target foreign-occupied seg1/0; got {moves:?}"
            );
        }
        // Destinations must not collide with foreign x.
        let (x_seg, x_off) = l2_offset(&auth, "m", b"x");
        for m in &moves {
            assert!(
                !(m.to_segment == x_seg && m.to_offset == x_off),
                "move onto foreign slot; {m:?}"
            );
        }
    }

    // --- S3(issue #74):自动 defrag 阈值(配置门控) ---

    /// 碎片率度量:致密 → 0;有洞 → 1;混合按比例;冻结段不计入。
    #[test]
    fn s3_l2_fragmentation_ratio_metric() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let pk = PoolKind::Target as i32;
        let slot = 100u64;
        // seg1 致密(0,100);seg2 有洞(0,200)。
        auth.register(
            "n0",
            &prefix(&[b"a", b"b", b"c", b"d"]),
            vec![
                meta_l2_at("m", b"a", 1, 0),
                meta_l2_at("m", b"b", 1, 100),
                meta_l2_at("m", b"c", 2, 0),
                meta_l2_at("m", b"d", 2, 200),
            ],
        )
        .unwrap();
        let ratio = auth.l2_fragmentation_ratio("m", "", pk, slot);
        assert_eq!(ratio, 0.5, "1 致密 + 1 有洞 → 1/2;got {ratio}");

        // 冻结 seg2(其上任一块 ref>0)→ 该段不计入,只剩致密 seg1 → 0。
        auth.report_ref(&delta("m", b"c", 1)).unwrap();
        assert_eq!(auth.l2_fragmentation_ratio("m", "", pk, slot), 0.0);
    }

    /// 门控语义:默认关;阈值未达不触发;达到触发且计划非空。
    #[tokio::test]
    async fn s3_auto_defrag_threshold_gated() {
        use std::sync::Mutex as StdMutex;

        #[derive(Default)]
        struct DefragRec {
            calls: StdMutex<Vec<usize>>,
        }
        impl DefragSink for DefragRec {
            fn defrag_plan(&self, _m: &str, _r: &str, _pk: i32, moves: Vec<DefragMove>) {
                self.calls.lock().unwrap().push(moves.len());
            }
        }

        let fragmented = || {
            vec![
                meta_l2_at("m", b"a", 1, 0),
                meta_l2_at("m", b"b", 1, 8192), // 非致密(slot=4096)
            ]
        };
        let register = |cp: &ControlPlane, blocks: Vec<BlockMeta>| {
            let cp = cp.clone();
            async move {
                cp.register_blocks(Request::new(RegisterBlocksRequest {
                    node_id: "n0".into(),
                    prefix_hashes: prefix(&[b"a", b"b"]),
                    blocks,
                }))
                .await
                .unwrap()
                .into_inner()
            }
        };

        // 默认 None=关:碎片布局也不触发。
        let sink = Arc::new(DefragRec::default());
        let cp = ControlPlane::default().with_defrag_sink(sink.clone());
        {
            let mut auth = cp.write_authority().unwrap();
            ensure_model(&mut auth, "m");
        }
        assert!(register(&cp, fragmented()).await.ok);
        assert_eq!(sink.calls.lock().unwrap().len(), 0, "默认关闭不得触发");

        // 阈值未达(1/3 < 0.5)不触发。
        let sink2 = Arc::new(DefragRec::default());
        let cp2 = ControlPlane::default()
            .with_defrag_sink(sink2.clone())
            .with_auto_defrag_threshold(Some(0.5));
        {
            let mut auth = cp2.write_authority().unwrap();
            ensure_model(&mut auth, "m");
        }
        let ack = cp2
            .register_blocks(Request::new(RegisterBlocksRequest {
                node_id: "n0".into(),
                prefix_hashes: prefix(&[b"a", b"b", b"c", b"d", b"e", b"f"]),
                blocks: vec![
                    meta_l2_at("m", b"a", 1, 0),
                    meta_l2_at("m", b"b", 1, 8192), // seg1 有洞
                    meta_l2_at("m", b"c", 2, 0),
                    meta_l2_at("m", b"d", 2, 4096), // seg2 致密
                    meta_l2_at("m", b"e", 3, 0),
                    meta_l2_at("m", b"f", 3, 4096), // seg3 致密
                ],
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(ack.ok, "{}", ack.err);
        assert_eq!(
            sink2.calls.lock().unwrap().len(),
            0,
            "碎片率 1/3 < 阈值 0.5 不得触发"
        );
        // 「阈值未达」分支自证:直接钉比值(3 段 1 段有洞 → 1/3)。
        let ratio = cp2
            .read_authority()
            .unwrap()
            .l2_fragmentation_ratio("m", "", 0, 0);
        assert!(
            (ratio - 1.0 / 3.0).abs() < 1e-9,
            "碎片率应为 1/3,实得 {ratio}"
        );

        // 达到阈值(1/1 ≥ 0.5)→ 触发一次且计划非空。
        let sink3 = Arc::new(DefragRec::default());
        let cp3 = ControlPlane::default()
            .with_defrag_sink(sink3.clone())
            .with_auto_defrag_threshold(Some(0.5));
        {
            let mut auth = cp3.write_authority().unwrap();
            ensure_model(&mut auth, "m");
        }
        assert!(register(&cp3, fragmented()).await.ok);
        let calls = sink3.calls.lock().unwrap();
        assert_eq!(calls.len(), 1, "达阈值应触发一次");
        assert!(calls[0] > 0, "触发的计划必须非空");
    }

    /// PR #77 review(高):DefragSink 必须在 Authority 写锁**外**投递——sink
    /// 回调 CP(读锁)或等待 agent 时,锁内调用即全局阻塞/死锁。本测试 sink 内
    /// 立刻取 CP 读锁;RPC 放独立阻塞线程 + 超时,回归(锁内投递)时超时失败
    /// 而非挂死 CI。WarmupSink 三处调用点(register_blocks/join_shard_node/
    /// report_hits)同修法、同约束。
    #[tokio::test]
    async fn s3_defrag_sink_delivered_outside_authority_lock() {
        use std::sync::Mutex as StdMutex;
        use std::time::Duration;

        struct ReentrantSink(StdMutex<Option<ControlPlane>>);
        impl DefragSink for ReentrantSink {
            fn defrag_plan(&self, _m: &str, _r: &str, _pk: i32, moves: Vec<DefragMove>) {
                assert!(!moves.is_empty());
                let cp = self.0.lock().unwrap().clone().unwrap();
                // 锁内投递(回归)时:调用方持写锁,此处读锁同线程死锁。
                let _guard = cp.read_authority().expect("read authority");
            }
        }

        let sink = Arc::new(ReentrantSink(StdMutex::new(None)));
        let cp = ControlPlane::default()
            .with_defrag_sink(sink.clone())
            .with_auto_defrag_threshold(Some(0.5));
        *sink.0.lock().unwrap() = Some(cp.clone());
        {
            let mut auth = cp.write_authority().unwrap();
            ensure_model(&mut auth, "m");
        }

        let cp2 = cp.clone();
        let handle = tokio::task::spawn_blocking(move || {
            let rt = tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap();
            rt.block_on(async move {
                cp2.register_blocks(Request::new(RegisterBlocksRequest {
                    node_id: "n0".into(),
                    prefix_hashes: prefix(&[b"a", b"b"]),
                    blocks: vec![
                        meta_l2_at("m", b"a", 1, 0),
                        meta_l2_at("m", b"b", 1, 8192), // seg1 有洞 → 碎片率 1/1 ≥ 0.5
                    ],
                }))
                .await
                .unwrap()
                .into_inner()
            })
        });
        let ack = tokio::time::timeout(Duration::from_secs(10), handle)
            .await
            .expect("超时:DefragSink 在写锁内投递(死锁回归)")
            .unwrap();
        assert!(ack.ok, "{}", ack.err);
    }

    /// PauseBackground 期间不触发(与 promote/demote/GC 共享暂停语义)。
    #[tokio::test]
    async fn s3_auto_defrag_paused_skipped() {
        use std::sync::Mutex as StdMutex;

        #[derive(Default)]
        struct DefragRec {
            calls: StdMutex<Vec<usize>>,
        }
        impl DefragSink for DefragRec {
            fn defrag_plan(&self, _m: &str, _r: &str, _pk: i32, moves: Vec<DefragMove>) {
                self.calls.lock().unwrap().push(moves.len());
            }
        }

        let sink = Arc::new(DefragRec::default());
        let cp = ControlPlane::default()
            .with_defrag_sink(sink.clone())
            .with_auto_defrag_threshold(Some(0.5));
        {
            let mut auth = cp.write_authority().unwrap();
            ensure_model(&mut auth, "m");
        }
        cp.set_background_paused(true);
        let ack = cp
            .register_blocks(Request::new(RegisterBlocksRequest {
                node_id: "n0".into(),
                prefix_hashes: prefix(&[b"a", b"b"]),
                blocks: vec![meta_l2_at("m", b"a", 1, 0), meta_l2_at("m", b"b", 1, 8192)],
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(ack.ok, "{}", ack.err);
        assert_eq!(
            sink.calls.lock().unwrap().len(),
            0,
            "PauseBackground 期间不得触发"
        );
    }

    // --- P4.9: consistent-hash sharding ---

    #[test]
    fn p49_shard_membership_get_map() {
        let mut auth = Authority::default();
        auth.join_shard_node("n0", 16).unwrap();
        auth.join_shard_node("n1", 16).unwrap();
        let map = auth.shard_map();
        assert_eq!(map.nodes.len(), 2);
        assert!(map.generation >= 2);
        assert!(auth.shard_owner(b"any-key").is_some());
    }

    #[test]
    fn p49_expand_minimal_migration() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        auth.join_shard_node("n0", 32).unwrap();
        auth.join_shard_node("n1", 32).unwrap();
        // Register many blocks so join n2 produces measurable (but bounded) moves.
        for i in 0..80u32 {
            let h = format!("b{i:03}").into_bytes();
            auth.register("n0", &prefix(&[&h]), vec![meta("m", &h)])
                .unwrap();
        }
        let (_map, migs) = auth.join_shard_node("n2", 32).unwrap();
        assert!(
            !migs.is_empty() && migs.len() < 80,
            "expand should move a subset, got {}",
            migs.len()
        );
        assert!(migs.iter().all(|m| m.to_node == "n2"));
        assert!(migs
            .iter()
            .all(|m| m.from_node == "n0" || m.from_node == "n1"));
        assert!(migs.iter().all(|m| !m.push_l2_first));
    }

    /// P7 收口(方案 Z):ReportHits 喂 hit_count → warmup_plan 按热度选块,
    /// 已在目标 L0 的块排除;未知块上报跳过(best-effort)。
    #[test]
    fn p7_report_hits_feeds_warmup_plan() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"hot", b"cold"]);
        auth.register("n0", &full, vec![meta("m", b"hot"), meta("m", b"cold")])
            .unwrap();
        let id_of = |h: &[u8]| KvBlockId {
            model_id: "m".into(),
            block_hash: h.to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
            revision: String::new(),
        };
        auth.report_hits(
            "router-0",
            &[
                id_of(b"hot"),
                id_of(b"hot"),
                id_of(b"hot"),
                id_of(b"cold"),
                id_of(b"ghost"), // 未注册 → 跳过
            ],
        );
        let key = NamespaceKey::new("m", "");
        assert_eq!(auth.hit_count(&key, PoolKind::Target as i32, b"hot"), 3);
        assert_eq!(auth.hit_count(&key, PoolKind::Target as i32, b"cold"), 1);
        assert_eq!(auth.hit_count(&key, PoolKind::Target as i32, b"ghost"), 0);

        let plan = auth.warmup_plan("n1", 10);
        assert_eq!(plan.len(), 2);
        assert_eq!(plan[0].block_hash, b"hot".to_vec(), "按 hit_count 降序");

        // hot 已在 n0 L0 → warm 到 n0 时排除。
        auth.publish_location(
            "m",
            "",
            PoolKind::Target as i32,
            b"hot",
            Tier::L0,
            "n0",
            true,
        )
        .unwrap();
        let plan0 = auth.warmup_plan("n0", 10);
        assert_eq!(plan0.len(), 1);
        assert_eq!(plan0[0].block_hash, b"cold".to_vec());
    }

    /// P7.6(B2-b):跟随流量——per-(block,node) 计数过阈值触发放置,滞回不重复。
    #[test]
    fn p76_follow_traffic_threshold_and_hysteresis() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"solo"]);
        auth.register("n0", &full, vec![meta("m", b"solo")])
            .unwrap();
        let id_of = |h: &[u8]| KvBlockId {
            model_id: "m".into(),
            block_hash: h.to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
            revision: String::new(),
        };
        let key = NamespaceKey::new("m", "");

        // 第 1 次命中:未达阈值(2)→ 无计划。
        let plans = auth.report_hits("n5", &[id_of(b"solo")]);
        assert!(plans.is_empty());
        assert_eq!(
            auth.hit_count_on(&key, PoolKind::Target as i32, b"solo", "n5"),
            1
        );

        // 第 2 次命中:达阈值 → 对 n5 触发放置。
        let plans = auth.report_hits("n5", &[id_of(b"solo")]);
        assert_eq!(plans.len(), 1);
        assert_eq!(plans[0].0, "n5");
        assert_eq!(plans[0].1.len(), 1);
        assert_eq!(plans[0].1[0].block_hash, b"solo".to_vec());

        // 滞回:继续命中不重复下发。
        let plans = auth.report_hits("n5", &[id_of(b"solo")]);
        assert!(plans.is_empty(), "已下发过 (block,node) 不重复触发");
        assert_eq!(
            auth.hit_count_on(&key, PoolKind::Target as i32, b"solo", "n5"),
            3
        );

        // 同一块在另一节点独立计数/触发。
        let plans = auth.report_hits("n7", &[id_of(b"solo")]);
        assert!(plans.is_empty());
        let plans = auth.report_hits("n7", &[id_of(b"solo")]);
        assert_eq!(plans.len(), 1);
        assert_eq!(plans[0].0, "n7");
        assert_eq!(auth.hit_count(&key, PoolKind::Target as i32, b"solo"), 5);
    }

    /// P7.6(B2-b):前缀祖先共置——放置块 c 时把未放置的祖先链 a、b 一并放
    /// (D-direct 需要整链),根→叶序。
    #[test]
    fn p76_follow_traffic_colocates_ancestors() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"a", b"b", b"c"]);
        auth.register(
            "n0",
            &full,
            vec![meta("m", b"a"), meta("m", b"b"), meta("m", b"c")],
        )
        .unwrap();
        let id_of = |h: &[u8]| KvBlockId {
            model_id: "m".into(),
            block_hash: h.to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
            revision: String::new(),
        };
        // a 已在 n5 L0 → 共置只补 b、c。
        auth.publish_location("m", "", PoolKind::Target as i32, b"a", Tier::L0, "n5", true)
            .unwrap();

        let plans = auth.report_hits("n5", &[id_of(b"c")]);
        assert!(plans.is_empty());
        let plans = auth.report_hits("n5", &[id_of(b"c")]);
        assert_eq!(plans.len(), 1);
        let hashes: Vec<Vec<u8>> = plans[0].1.iter().map(|i| i.block_hash.clone()).collect();
        assert_eq!(
            hashes,
            vec![b"b".to_vec(), b"c".to_vec()],
            "祖先共置,跳过已在 n5 L0 的 a,根→叶序"
        );
    }

    /// P7.6(B2-b):块已在该节点 L0 → 不触发(且不标记,未来被驱逐可自愈)。
    #[test]
    fn p76_follow_traffic_skips_already_l0() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"solo"]);
        auth.register("n0", &full, vec![meta("m", b"solo")])
            .unwrap();
        auth.publish_location(
            "m",
            "",
            PoolKind::Target as i32,
            b"solo",
            Tier::L0,
            "n5",
            true,
        )
        .unwrap();
        let id = KvBlockId {
            model_id: "m".into(),
            block_hash: b"solo".to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
            revision: String::new(),
        };
        for _ in 0..3 {
            let plans = auth.report_hits("n5", std::slice::from_ref(&id));
            assert!(plans.is_empty(), "已在 L0 不重复放");
        }
        let key = NamespaceKey::new("m", "");
        assert_eq!(
            auth.hit_count_on(&key, PoolKind::Target as i32, b"solo", "n5"),
            3
        );
    }

    /// S1(issue #74):计划→放置→L0 驱逐→同块同节点可再触发(自愈回归)。
    /// 修复前 `publish_location_at` 剥 L0 不清滞回标记,已计划块永远无法再触发
    /// 对该节点的放置;修复后 L0 驱逐(present=false)连带清 (flat,node) 标记。
    #[test]
    fn s1_l0_evict_clears_placement_mark_and_retriggers() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"solo"]);
        auth.register("n0", &full, vec![meta("m", b"solo")])
            .unwrap();
        let pk = PoolKind::Target as i32;
        let id = KvBlockId {
            model_id: "m".into(),
            block_hash: b"solo".to_vec(),
            pool_kind: pk,
            scope: "public".into(),
            revision: String::new(),
        };

        // 过阈值 → 下发计划(记滞回标记);再命中 → 滞回不重复。
        assert!(auth.report_hits("n5", std::slice::from_ref(&id)).is_empty());
        let plans = auth.report_hits("n5", std::slice::from_ref(&id));
        assert_eq!(plans.len(), 1);
        assert_eq!(plans[0].0, "n5");
        assert!(auth.report_hits("n5", std::slice::from_ref(&id)).is_empty());

        // 放置完成(agent PlaceBlocks → PublishLocation)→ 之后 L0 被驱逐。
        auth.publish_location("m", "", pk, b"solo", Tier::L0, "n5", true)
            .unwrap();
        auth.publish_location("m", "", pk, b"solo", Tier::L0, "n5", false)
            .unwrap();

        // 自愈:滞回标记已随驱逐清除,再次命中(计数仍 ≥ 阈值)可再触发。
        let plans = auth.report_hits("n5", std::slice::from_ref(&id));
        assert_eq!(
            plans.len(),
            1,
            "L0 驱逐后滞回标记必须清除,同块同节点可再触发"
        );
        assert_eq!(plans[0].0, "n5");
        assert_eq!(plans[0].1[0].block_hash, b"solo".to_vec());
        // 再触发后滞回重建:继续命中不重复下发。
        assert!(auth.report_hits("n5", std::slice::from_ref(&id)).is_empty());
    }

    /// P7.6(B2-a):HRW 预放置——空环无计划;ready 节点集确定家节点;
    /// 链不在家节点 L0 的块全量预放;滞回不重复;已在家节点 L0 零动作。
    #[test]
    fn p76_preplace_on_register_hrw_home() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        let full = prefix(&[b"a", b"b"]);
        let pk = PoolKind::Target as i32;

        // 空环 → None。
        assert!(auth.preplace_on_register("m", "", pk, &full).is_none());

        auth.register("n0", &full, vec![meta("m", b"a"), meta("m", b"b")])
            .unwrap();
        auth.join_shard_node("n0", 16).unwrap();
        // 唯一 ready 节点 → 家节点必为 n0;块仅 L2 → 整链预放。
        let (home, plan) = auth.preplace_on_register("m", "", pk, &full).unwrap();
        assert_eq!(home, "n0");
        let hashes: Vec<Vec<u8>> = plan.iter().map(|i| i.block_hash.clone()).collect();
        assert_eq!(hashes, vec![b"a".to_vec(), b"b".to_vec()]);

        // 滞回:重复调用不再产生计划。
        assert!(auth.preplace_on_register("m", "", pk, &full).is_none());

        // 新链已在家节点 L0(生产节点==家节点的稳态)→ 零动作。
        let full2 = prefix(&[b"x"]);
        let mut meta_x = meta("m", b"x");
        meta_x.locations.push(Location {
            tier: Tier::L0 as i32,
            node_id: "n0".into(),
            segment_id: 1,
            offset: 0,
        });
        auth.register("n0", &full2, vec![meta_x]).unwrap();
        assert!(auth.preplace_on_register("m", "", pk, &full2).is_none());
    }

    /// P7.6(B2-b):ReportHits handler 接线——过阈值经 WarmupSink 下发,
    /// 滞回后不再下发。
    #[tokio::test]
    async fn p76_report_hits_handler_follow_traffic_warm() {
        use std::sync::Mutex as StdMutex;

        #[derive(Default)]
        struct RecSink {
            calls: StdMutex<Vec<(String, Vec<Vec<u8>>)>>,
        }
        impl WarmupSink for RecSink {
            fn warm(&self, target: &str, ids: Vec<KvBlockId>) {
                self.calls.lock().unwrap().push((
                    target.to_string(),
                    ids.into_iter().map(|i| i.block_hash).collect(),
                ));
            }
        }

        let sink = Arc::new(RecSink::default());
        let cp = ControlPlane::default().with_warmup_sink(sink.clone());
        {
            let mut auth = cp.write_authority().unwrap();
            ensure_model(&mut auth, "m");
            let full = prefix(&[b"a", b"c"]);
            auth.register("n0", &full, vec![meta("m", b"a"), meta("m", b"c")])
                .unwrap();
        }
        let id_of = |h: &[u8]| KvBlockId {
            model_id: "m".into(),
            block_hash: h.to_vec(),
            pool_kind: PoolKind::Target as i32,
            scope: "public".into(),
            revision: String::new(),
        };
        for _ in 0..2 {
            cp.report_hits(Request::new(ReportHitsRequest {
                node_id: "n5".into(),
                ids: vec![id_of(b"c")],
            }))
            .await
            .unwrap();
        }
        // 祖先共置:a、c 一并下发到 n5。
        {
            let calls = sink.calls.lock().unwrap();
            assert_eq!(calls.len(), 1);
            assert_eq!(calls[0].0, "n5");
            assert_eq!(calls[0].1, vec![b"a".to_vec(), b"c".to_vec()]);
        }
        // 滞回:再次过阈值不下发。
        for _ in 0..2 {
            cp.report_hits(Request::new(ReportHitsRequest {
                node_id: "n5".into(),
                ids: vec![id_of(b"c")],
            }))
            .await
            .unwrap();
        }
        let calls = sink.calls.lock().unwrap();
        assert_eq!(calls.len(), 1, "滞回:不重复下发");
    }

    /// P7.6(B2-a):RegisterBlocks handler 接线——成功后按 HRW 对家节点预放置。
    #[tokio::test]
    async fn p76_register_handler_hrw_preplace() {
        use std::sync::Mutex as StdMutex;

        #[derive(Default)]
        struct RecSink {
            calls: StdMutex<Vec<(String, Vec<Vec<u8>>)>>,
        }
        impl WarmupSink for RecSink {
            fn warm(&self, target: &str, ids: Vec<KvBlockId>) {
                self.calls.lock().unwrap().push((
                    target.to_string(),
                    ids.into_iter().map(|i| i.block_hash).collect(),
                ));
            }
        }

        let sink = Arc::new(RecSink::default());
        let cp = ControlPlane::default().with_warmup_sink(sink.clone());
        {
            let mut auth = cp.write_authority().unwrap();
            ensure_model(&mut auth, "m");
            // 环先就位(ready 节点集 = [n0]),register 才有预放置目标。
            auth.join_shard_node("n0", 16).unwrap();
        }
        let resp = cp
            .register_blocks(Request::new(RegisterBlocksRequest {
                node_id: "n0".into(),
                prefix_hashes: prefix(&[b"a", b"b"]),
                blocks: vec![meta("m", b"a"), meta("m", b"b")],
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(resp.ok, "register 应成功:{}", resp.err);
        let calls = sink.calls.lock().unwrap();
        assert_eq!(calls.len(), 1, "唯一 ready 节点 → 家节点 n0,整链预放");
        assert_eq!(calls[0].0, "n0");
        assert_eq!(calls[0].1, vec![b"a".to_vec(), b"b".to_vec()]);
    }

    /// P7 收口(方案 Z):join 后 warmup 由池侧自主发起(WarmupSink),
    /// 不经 Router PlaceBlocks。
    #[tokio::test]
    async fn p7_join_triggers_pool_side_warmup() {
        use std::sync::Mutex as StdMutex;

        #[derive(Default)]
        struct RecSink {
            calls: StdMutex<Vec<(String, Vec<Vec<u8>>)>>,
        }
        impl WarmupSink for RecSink {
            fn warm(&self, target: &str, ids: Vec<KvBlockId>) {
                self.calls.lock().unwrap().push((
                    target.to_string(),
                    ids.into_iter().map(|i| i.block_hash).collect(),
                ));
            }
        }

        let sink = Arc::new(RecSink::default());
        let cp = ControlPlane::default().with_warmup_sink(sink.clone());
        {
            let mut auth = cp.write_authority().unwrap();
            ensure_model(&mut auth, "m");
            let full = prefix(&[b"hot"]);
            auth.register("n0", &full, vec![meta("m", b"hot")]).unwrap();
        }
        cp.report_hits(Request::new(ReportHitsRequest {
            node_id: "router-0".into(),
            ids: vec![KvBlockId {
                model_id: "m".into(),
                block_hash: b"hot".to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
                revision: String::new(),
            }],
        }))
        .await
        .unwrap();

        let resp = cp
            .join_shard_node(Request::new(JoinShardNodeRequest {
                node_id: "n1".into(),
                vnode_count: 16,
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(resp.ok);

        let calls = sink.calls.lock().unwrap();
        assert_eq!(calls.len(), 1, "join 应触发一次池侧 warmup");
        assert_eq!(calls[0].0, "n1");
        assert_eq!(calls[0].1, vec![b"hot".to_vec()]);
    }

    /// WarmupSink 与 DefragSink 同约束(见 s3_defrag_sink_delivered_outside_authority_lock):
    /// 投递必须在 Authority 写锁释放后——sink 内回调 CP(读锁)时,锁内调用即
    /// 同线程死锁。本测试在 warm() 内取 CP 读锁;RPC 放独立阻塞线程 + 超时,
    /// 回归(锁内投递)时超时失败而非挂死 CI。覆盖 join_shard_node 调用点
    /// (register_blocks/report_hits 同构,锁外投递由同一重构保证)。
    #[tokio::test]
    async fn warmup_sink_delivered_outside_authority_lock() {
        use std::sync::Mutex as StdMutex;
        use std::time::Duration;

        struct ReentrantWarm(StdMutex<Option<ControlPlane>>);
        impl WarmupSink for ReentrantWarm {
            fn warm(&self, _target: &str, ids: Vec<KvBlockId>) {
                assert!(!ids.is_empty());
                let cp = self.0.lock().unwrap().clone().unwrap();
                // 锁内投递(回归)时:调用方持写锁,此处读锁同线程死锁。
                let _guard = cp.read_authority().expect("read authority");
            }
        }

        let sink = Arc::new(ReentrantWarm(StdMutex::new(None)));
        let cp = ControlPlane::default().with_warmup_sink(sink.clone());
        *sink.0.lock().unwrap() = Some(cp.clone());
        {
            let mut auth = cp.write_authority().unwrap();
            ensure_model(&mut auth, "m");
            let full = prefix(&[b"hot"]);
            auth.register("n0", &full, vec![meta("m", b"hot")]).unwrap();
        }
        cp.report_hits(Request::new(ReportHitsRequest {
            node_id: "router-0".into(),
            ids: vec![KvBlockId {
                model_id: "m".into(),
                block_hash: b"hot".to_vec(),
                pool_kind: PoolKind::Target as i32,
                scope: "public".into(),
                revision: String::new(),
            }],
        }))
        .await
        .unwrap();

        let cp2 = cp.clone();
        let handle = tokio::task::spawn_blocking(move || {
            let rt = tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap();
            rt.block_on(async move {
                cp2.join_shard_node(Request::new(JoinShardNodeRequest {
                    node_id: "n1".into(),
                    vnode_count: 16,
                }))
                .await
                .unwrap()
                .into_inner()
            })
        });
        let resp = tokio::time::timeout(Duration::from_secs(5), handle)
            .await
            .expect("join_shard_node timed out (sink 疑似在锁内投递)")
            .unwrap();
        assert!(resp.ok);
    }

    #[test]
    fn p49_drain_push_l2_and_remove() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        auth.join_shard_node("n0", 32).unwrap();
        auth.join_shard_node("n1", 32).unwrap();
        // Place blocks with L2 on n1.
        for i in 0..20u32 {
            let h = format!("d{i:02}").into_bytes();
            let mut m = meta("m", &h);
            m.locations[0].node_id = "n1".into();
            auth.register("n1", &prefix(&[&h]), vec![m]).unwrap();
        }
        let (map, migs, push) = auth.drain_shard_node("n1").unwrap();
        assert!(map.nodes.iter().any(|n| n.node_id == "n1" && n.draining));
        assert!(!push.is_empty(), "Drain must list L2 push candidates");
        assert!(migs.iter().all(|m| m.from_node == "n1" && m.push_l2_first));
        // After drain, no key should own on n1.
        for id in &push {
            assert_ne!(auth.shard_owner(&id.block_hash).as_deref(), Some("n1"));
        }
        // Ownership remap ≠ physical done: remove must fail while L2 remains.
        let err = auth.remove_shard_node("n1").unwrap_err();
        assert!(
            err.contains("placement"),
            "expected placement gate, got {err}"
        );
        // Simulate migration completion: clear n1 L2 from the location view.
        for id in &push {
            auth.publish_location(
                &id.model_id,
                &id.revision,
                id.pool_kind,
                &id.block_hash,
                Tier::L2,
                "n1",
                false,
            )
            .unwrap();
        }
        auth.remove_shard_node("n1").unwrap();
        let map2 = auth.shard_map();
        assert!(!map2.nodes.iter().any(|n| n.node_id == "n1"));
    }

    /// RemoveShardNode must not succeed solely because ownership left the ring.
    #[test]
    fn p49_remove_refuses_while_l2_present() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        auth.join_shard_node("n0", 16).unwrap();
        auth.join_shard_node("n1", 16).unwrap();
        let mut m = meta("m", b"stuck");
        m.locations[0].node_id = "n1".into();
        auth.register("n1", &prefix(&[b"stuck"]), vec![m]).unwrap();
        auth.drain_shard_node("n1").unwrap();
        assert!(auth
            .remove_shard_node("n1")
            .unwrap_err()
            .contains("placement"));
        // Still in shard map as draining.
        assert!(auth
            .shard_map()
            .nodes
            .iter()
            .any(|n| n.node_id == "n1" && n.draining));
    }

    /// Drain must keep TARGET and DRAFT blocks separate even if their flat hash matches.
    #[test]
    fn p49_drain_keeps_pool_kind_identities() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        auth.join_shard_node("n0", 16).unwrap();
        auth.join_shard_node("n1", 16).unwrap();

        let mut target = meta("m", b"same");
        target.locations[0].node_id = "n1".into();
        auth.register("n1", &prefix(&[b"same"]), vec![target])
            .unwrap();

        let mut draft = meta("m", b"same");
        draft.id.as_mut().unwrap().pool_kind = PoolKind::Draft as i32;
        draft.locations[0].node_id = "n1".into();
        auth.register("n1", &prefix(&[b"same"]), vec![draft])
            .unwrap();

        let (_map, _migs, push) = auth.drain_shard_node("n1").unwrap();
        let same: Vec<_> = push
            .iter()
            .filter(|id| id.model_id == "m" && id.block_hash == b"same".to_vec())
            .map(|id| id.pool_kind)
            .collect();
        assert_eq!(
            same.len(),
            2,
            "TARGET and DRAFT with same hash must both be pushed: {push:?}"
        );
        assert!(same.contains(&(PoolKind::Target as i32)));
        assert!(same.contains(&(PoolKind::Draft as i32)));
    }

    /// relocate_in_view updates segment/offset; PauseBackground flips flag.
    #[test]
    fn p48_relocate_and_pause_flag() {
        let mut auth = Authority::default();
        ensure_model(&mut auth, "m");
        auth.register("n0", &prefix(&[b"p"]), vec![meta("m", b"p")])
            .unwrap();
        auth.relocate_in_view(
            "m",
            "",
            PoolKind::Target as i32,
            b"p",
            Tier::L2,
            "n0",
            7,
            128,
        )
        .unwrap();
        assert_eq!(l2_offset(&auth, "m", b"p"), (7, 128));

        let cp = ControlPlane::new();
        assert!(!cp.background_paused());
        cp.set_background_paused(true);
        assert!(cp.background_paused());
        cp.set_background_paused(false);
        assert!(!cp.background_paused());
    }
}
