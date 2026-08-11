//! 位置视图权威：每 `(model_id, revision)` 一个命名空间；
//! 其内按 `pool_kind`(TARGET/DRAFT) 各持一棵 `BlockRegistry`（schema 寻址含 pool_kind）。
//!
//! 参考:Dynamo `BlockRegistry` / `InactiveIndex`；驱逐主路径 =
//! `LineageBackend::with_frequency`（只驱叶子 ≈ 前缀亲和 + TinyLFU 冷叶优先 ≈ LFU-Aging）。
//! 不用 `BlockManager`/`BlockStore`——因此必须自己守 inactive 上界：
//! `report_ref` 满容只 skip insert（对齐 Dynamo `inactive.insert`）；
//! 压力 `allocate` 走生产准入路径 `commit_admit`→`evict_inactive_n`
//! （RegisterBlocks 命中配额时触发）；`evict_n` 是测试钩子（对齐 `allocate_atomic`）。
//! EventsManager 不接线。
//!
//! P4.5:显式 `RegisterModel` / `DeregisterModel`；禁止懒建命名空间。
//! 下线 = 整表 drop（强句柄释放 → radix Weak 失效 ≈ 按命名空间剪枝）。
//! P4.6:软/硬配额 + 借用 + 触硬 `BackpressureSignal`（RegisterBlocks 拒扩容写入）。
//! **关键差异**(相对 Dynamo):lake 一等 `(model_id, revision)` + `TARGET|DRAFT` 同池寻址，
//! 不能共用单 registry 的 flat→entry 表。
//! **关键差异**(相对 Mooncake tenant quota / LMCache QuotaManager):见 `quota.rs`。

use std::collections::HashMap;
use std::sync::Arc;

use kvbm_logical::registry::BlockRegistrationHandle;
use kvbm_logical::{
    BlockId, BlockRegistry, FrequencyTrackingCapacity, InactiveIndex, LineageBackend, SequenceHash,
};

use crate::hash_chain::lineage_from_prefix;
use crate::quota::{self, AdmitWrite};
use crate::tier::{TierL0, TierL1, TierL2};
use lake_proto::lake::*;

/// Result of [`Authority::register`] after quota admission.
#[derive(Debug, Clone, PartialEq)]
pub enum RegisterStatus {
    Accepted,
    /// Hard quota would be exceeded — write rejected; signal for gateway.
    RejectedHardQuota(BackpressureSignal),
}

/// Pure admission plan (review #1): reject has no victims; accept lists mutations.
#[derive(Debug, Clone)]
enum AdmitPlan {
    Accept {
        own_evict_n: usize,
        reclaim_bytes: i64,
    },
    Reject(BackpressureSignal),
}

impl AdmitPlan {
    fn status(&self) -> RegisterStatus {
        match self {
            AdmitPlan::Accept { .. } => RegisterStatus::Accepted,
            AdmitPlan::Reject(bp) => RegisterStatus::RejectedHardQuota(bp.clone()),
        }
    }
}

const INACTIVE_CAP: usize = 4096;
/// Same threshold shape as `MultiLruBackend` / Frequency LeafPolicy.
const FREQ_THRESHOLDS: [u8; 3] = [3, 8, 15];

/// Control-plane model namespace key = `(model_id, revision)`.
/// Block identity within = `(pool_kind, block_hash)` → separate [`PoolView`].
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct NamespaceKey {
    pub model_id: String,
    pub revision: String,
}

impl NamespaceKey {
    pub fn new(model_id: impl Into<String>, revision: impl Into<String>) -> Self {
        Self {
            model_id: model_id.into(),
            revision: revision.into(),
        }
    }

    pub fn from_id(id: &KvBlockId) -> Self {
        Self::new(id.model_id.clone(), id.revision.clone())
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub(crate) struct RefTarget {
    pub(crate) key: NamespaceKey,
    pub(crate) pool_kind: i32,
    pub(crate) seq: SequenceHash,
}

/// Wire `POOL_UNSPECIFIED`(0) → TARGET. Reject unknown kinds.
pub fn resolve_pool_kind(raw: i32) -> Result<i32, String> {
    // POOL_UNSPECIFIED = 0 → TARGET (LookupPrefix / wire default).
    if raw == 0 {
        return Ok(PoolKind::Target as i32);
    }
    if raw == PoolKind::Target as i32 || raw == PoolKind::Draft as i32 {
        return Ok(raw);
    }
    Err(format!("unsupported pool_kind {raw}"))
}

pub(crate) struct Entry {
    pub(crate) seq_hash: SequenceHash,
    pub(crate) meta: BlockMeta,
    pub(crate) block_id: BlockId,
    /// Flat hashes from root through this block (inclusive). Checkpoint export
    /// needs this — PLH parent fragments alone cannot rebuild content hashes.
    pub(crate) prefix_chain: Vec<Vec<u8>>,
}

/// S4(issue #74):per-block ref 按 proto `RefKind` 分账(请求持有 / 写回未 durable)。
/// 驱逐冻结语义仍看**总数**(`total() > 0` 冻结不变);分账的价值是 kind 级
/// underflow 定位与可观测性(写回泄漏 vs 请求泄漏可区分)。
/// 参考:SGLang `radix_cache.py::inc_lock_ref/dec_lock_ref` 分 kind 持锁——
/// kind 只影响记账维度,不改变「任一持有即冻结」的判定;lake 同构。
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) struct RefAccounts {
    pub(crate) request: i64,
    pub(crate) writeback: i64,
}

impl RefAccounts {
    pub(crate) fn total(&self) -> i64 {
        self.request + self.writeback
    }

    pub(crate) fn bucket_mut(&mut self, kind: RefKind) -> &mut i64 {
        match kind {
            RefKind::Request => &mut self.request,
            RefKind::Writeback => &mut self.writeback,
            // 入账前必经 `resolve_ref_kind` 过滤,此处不可达。
            _ => unreachable!("unsupported ref_kind filtered by resolve_ref_kind"),
        }
    }

    pub(crate) fn bucket(&self, kind: RefKind) -> i64 {
        match kind {
            RefKind::Request => self.request,
            RefKind::Writeback => self.writeback,
            _ => unreachable!("unsupported ref_kind filtered by resolve_ref_kind"),
        }
    }
}

/// S4:入账 kind 解析——REQUEST/WRITEBACK 记账;IN_FLIGHT 预留未接线、
/// UNSPECIFIED 为缺省,一律显式报错(对齐 PR #47 显式错误哲学;agent 当前
/// 只上报 REQUEST/WRITEBACK,见 storage-agent `putend.rs`)。
pub(crate) fn resolve_ref_kind(raw: i32) -> Result<RefKind, String> {
    let kind = RefKind::try_from(raw).map_err(|_| format!("RefDelta: unknown ref_kind {raw}"))?;
    match kind {
        RefKind::Request | RefKind::Writeback => Ok(kind),
        _ => Err(format!(
            "RefDelta: unsupported ref_kind {} (want REQUEST/WRITEBACK)",
            kind.as_str_name()
        )),
    }
}

/// One Dynamo-shaped registry domain per `pool_kind`.
pub(crate) struct PoolView {
    pub(crate) registry: BlockRegistry,
    pub(crate) handles: HashMap<SequenceHash, BlockRegistrationHandle>,
    /// Flat content hash → entry **within this pool_kind**.
    pub(crate) by_flat: HashMap<Vec<u8>, Entry>,
    pub(crate) seq_to_flat: HashMap<SequenceHash, Vec<u8>>,
    pub(crate) inactive: Box<dyn InactiveIndex>,
    pub(crate) inactive_cap: usize,
    /// S4:per-block ref 按 kind 分账;冻结/驱逐判定看 `RefAccounts::total()`。
    pub(crate) global_refs: HashMap<SequenceHash, RefAccounts>,
    /// P7 收口:命中计数(ReportHits 喂入)。生产挂 radix 节点
    /// (SGLang `TreeNode.hit_count` 同款),原型平铺按 flat hash;
    /// 供扩容 warmup 选块 / 方案 Z 预放置复用。
    /// P7.6(B2):扩成 per-(block,node)——识别「热在哪个节点」,
    /// 供跟随流量预放置(`placement.rs`)。
    pub(crate) hit_counts: HashMap<Vec<u8>, HashMap<String, u32>>,
    /// P7.6(B2):放置滞回标记(已下发计划的 (block,node) 不重复下发)。
    pub(crate) placement_marks: crate::placement::PlacementMarks,
    next_block_id: BlockId,
}

impl PoolView {
    fn new(inactive_cap: usize) -> Self {
        let cap = inactive_cap.max(1);
        let tracker = FrequencyTrackingCapacity::Small.create_tracker();
        let registry = BlockRegistry::builder()
            .frequency_tracker(Arc::clone(&tracker) as _)
            .build();
        let inactive = Box::new(
            LineageBackend::with_frequency(cap, FREQ_THRESHOLDS, tracker)
                .expect("Lineage+Frequency thresholds"),
        );
        Self {
            registry,
            handles: HashMap::new(),
            by_flat: HashMap::new(),
            seq_to_flat: HashMap::new(),
            inactive,
            inactive_cap: cap,
            global_refs: HashMap::new(),
            hit_counts: HashMap::new(),
            placement_marks: crate::placement::PlacementMarks::default(),
            next_block_id: 1,
        }
    }

    fn alloc_block_id(&mut self) -> BlockId {
        let id = self.next_block_id;
        self.next_block_id = self.next_block_id.saturating_add(1);
        id
    }

    /// Drop up to `n` inactive victims; returns removed `(seq_hash, flat)` pairs
    /// (P6.2: callers turn them into INVALIDATED view events).
    fn drop_inactive_victims(&mut self, n: usize) -> Vec<(SequenceHash, Vec<u8>)> {
        let victims = self.inactive.allocate(n);
        let mut removed = Vec::new();
        for (seq, _bid) in victims {
            if self
                .global_refs
                .get(&seq)
                .copied()
                .unwrap_or_default()
                .total()
                > 0
            {
                continue;
            }
            self.handles.remove(&seq);
            self.global_refs.remove(&seq);
            if let Some(flat) = self.seq_to_flat.remove(&seq) {
                self.by_flat.remove(&flat);
                // 块生命周期终结,连带清理 P7.6 的 per-(block,node) 热度与
                // 放置滞回标记(否则池侧两张表随 GC 单调泄漏)。
                self.hit_counts.remove(&flat);
                self.placement_marks.retain(|(f, _)| f != &flat);
                removed.push((seq, flat));
            }
        }
        removed
    }
}

pub(crate) struct Namespace {
    pub(crate) descriptor: ModelDescriptor,
    /// `pool_kind` → independent radix / inactive / refs.
    pub(crate) pools: HashMap<i32, PoolView>,
    inactive_cap: usize,
    /// Authoritative durable-byte usage for this namespace (all pool_kinds).
    pub(crate) used_bytes: i64,
}

impl Namespace {
    fn new(descriptor: ModelDescriptor, inactive_cap: usize) -> Self {
        Self {
            descriptor,
            pools: HashMap::new(),
            inactive_cap: inactive_cap.max(1),
            used_bytes: 0,
        }
    }

    fn pool_mut(&mut self, pool_kind: i32) -> &mut PoolView {
        let cap = self.inactive_cap;
        self.pools
            .entry(pool_kind)
            .or_insert_with(|| PoolView::new(cap))
    }

    fn pool(&self, pool_kind: i32) -> Option<&PoolView> {
        self.pools.get(&pool_kind)
    }

    fn block_count(&self) -> usize {
        self.pools.values().map(|p| p.by_flat.len()).sum()
    }

    pub(crate) fn bytes_per_block(&self) -> i64 {
        self.descriptor
            .block_spec
            .as_ref()
            .map(|s| s.bytes_per_block as i64)
            .unwrap_or(0)
            .max(1)
    }

    fn quota(&self) -> Quota {
        quota::quota_or_default(self.descriptor.quota.as_ref())
    }

    fn inactive_len(&self) -> usize {
        self.pools.values().map(|p| p.inactive.len()).sum()
    }

    fn inactive_bytes(&self) -> i64 {
        (self.inactive_len() as i64).saturating_mul(self.bytes_per_block())
    }

    /// Evict up to `n` inactive victims across all pool_kinds;
    /// returns removed `(pool_kind, seq_hash, flat)` triples (P6.2: view events).
    fn evict_inactive_n(&mut self, n: usize) -> Vec<(i32, SequenceHash, Vec<u8>)> {
        if n == 0 {
            return Vec::new();
        }
        let bpb = self.bytes_per_block();
        let mut left = n;
        let mut removed: Vec<(i32, SequenceHash, Vec<u8>)> = Vec::new();
        // Stable order: TARGET then DRAFT then others.
        let mut kinds: Vec<i32> = self.pools.keys().copied().collect();
        kinds.sort_unstable();
        for pk in kinds {
            if left == 0 {
                break;
            }
            let Some(pool) = self.pools.get_mut(&pk) else {
                continue;
            };
            for (seq, flat) in pool.drop_inactive_victims(left) {
                left = left.saturating_sub(1);
                removed.push((pk, seq, flat));
            }
        }
        self.used_bytes = (self.used_bytes - (removed.len() as i64) * bpb).max(0);
        removed
    }
}

/// Immutable identity of a registered model (quota is mutable).
fn descriptor_identity_eq(a: &ModelDescriptor, b: &ModelDescriptor) -> bool {
    a.model_id == b.model_id
        && a.revision == b.revision
        && a.num_layers == b.num_layers
        && a.hash_algo == b.hash_algo
        && a.block_spec == b.block_spec
}

/// P6.2: removed `(pool_kind, _seq, flat)` triples → INVALIDATED view events.
fn invalidated_events_for(
    model_id: &str,
    revision: &str,
    removed: Vec<(i32, SequenceHash, Vec<u8>)>,
) -> Vec<ViewEvent> {
    removed
        .into_iter()
        .map(|(pk, _seq, flat)| {
            crate::view::invalidated_event(KvBlockId {
                model_id: model_id.into(),
                revision: revision.into(),
                pool_kind: pk,
                block_hash: flat,
                scope: "public".into(),
            })
        })
        .collect()
}

/// Process-local authority state.
pub struct Authority {
    pub(crate) namespaces: HashMap<NamespaceKey, Namespace>,
    inactive_cap: usize,
    /// Completed request barriers: `(request_id, node_id)`.
    pub(crate) completed_barriers: HashMap<String, String>,
    /// P4.6 mock pool durable capacity for borrow accounting.
    /// `0` = unlimited free (borrow always available until per-model hard).
    pool_capacity_bytes: i64,
    /// P4.7: incomplete-write orphans (Mooncake zombie analogue).
    pub(crate) orphans: HashMap<crate::reconcile::BlockKey, crate::reconcile::OrphanEntry>,
    /// P4.7: per-node ref holdings for dead-node reconcile / writeback leak.
    /// S4:与 global_refs 同口径按 kind 分账,死节点清账按 kind 精确回冲。
    pub(crate) node_refs: HashMap<String, HashMap<crate::reconcile::BlockKey, RefAccounts>>,
    /// P4.7: injectable clock (orphan TTL tests).
    pub(crate) now_ms: fn() -> u64,
    /// P4.7: last checkpoint seq.
    pub(crate) checkpoint_seq: u64,
    /// P4.9: consistent-hash shard ring (ownership; bytes migrate in P5).
    pub(crate) shard: crate::shard::ShardRing,
    /// P6.2: 视图事件日志(SubscribeView replay buffer,有界)。
    pub(crate) view_log: crate::view::ViewLog,
    /// P6.2: 本写调用累积的视图事件;handler 无论成败都 commit
    /// (事件反映**状态迁移**而非 RPC 成败——部分变异也要让镜像看到)。
    pub(crate) pending_view_events: Vec<ViewEvent>,
}

impl Default for Authority {
    fn default() -> Self {
        Self::with_inactive_cap(INACTIVE_CAP)
    }
}

impl Authority {
    pub fn with_inactive_cap(inactive_cap: usize) -> Self {
        Self {
            namespaces: HashMap::new(),
            inactive_cap: inactive_cap.max(1),
            completed_barriers: HashMap::new(),
            pool_capacity_bytes: 0,
            orphans: HashMap::new(),
            node_refs: HashMap::new(),
            now_ms: crate::reconcile::wall_now_ms,
            checkpoint_seq: 0,
            shard: crate::shard::ShardRing::new(crate::shard::DEFAULT_VNODE_COUNT),
            view_log: crate::view::ViewLog::default(),
            pending_view_events: Vec::new(),
        }
    }

    /// Set total pool capacity used for borrow free-space checks (P4.6 tests).
    pub fn set_pool_capacity_bytes(&mut self, bytes: i64) {
        self.pool_capacity_bytes = bytes.max(0);
    }

    pub fn pool_capacity_bytes(&self) -> i64 {
        self.pool_capacity_bytes
    }

    fn total_used_bytes(&self) -> i64 {
        self.namespaces.values().map(|n| n.used_bytes).sum()
    }

    /// P6.2: 提交本写调用累积的视图事件(若有)为一个带序号的 `ViewUpdate`。
    /// 单写者持写锁调用;提交序即广播序。
    pub fn commit_view_events(&mut self) -> Option<ViewUpdate> {
        if self.pending_view_events.is_empty() {
            return None;
        }
        let events = std::mem::take(&mut self.pending_view_events);
        Some(self.view_log.commit(events))
    }

    /// P6.2: 全量快照(`seq = 0`;接收方重置镜像)。确定性排序(测试可断言)。
    pub fn view_snapshot(&self) -> ViewUpdate {
        let mut keys: Vec<&NamespaceKey> = self.namespaces.keys().collect();
        keys.sort_by(|a, b| {
            a.model_id
                .cmp(&b.model_id)
                .then(a.revision.cmp(&b.revision))
        });
        let mut events = Vec::new();
        for k in keys {
            let ns = &self.namespaces[k];
            let mut pks: Vec<i32> = ns.pools.keys().copied().collect();
            pks.sort_unstable();
            for pk in pks {
                let pool = &ns.pools[&pk];
                let mut flats: Vec<&Vec<u8>> = pool.by_flat.keys().collect();
                flats.sort();
                for flat in flats {
                    let entry = &pool.by_flat[flat];
                    let id = entry.meta.id.clone().unwrap_or_else(|| KvBlockId {
                        model_id: k.model_id.clone(),
                        revision: k.revision.clone(),
                        pool_kind: pk,
                        block_hash: flat.clone(),
                        scope: "public".into(),
                    });
                    events.push(crate::view::upsert_event(
                        view_event::Kind::Registered,
                        id,
                        entry.meta.locations.clone(),
                        entry.meta.l3_present,
                        entry.meta.block_kind,
                    ));
                }
            }
        }
        ViewUpdate { seq: 0, events }
    }

    /// P6.2: resume 重放 `(from_seq, ...]`;`None` → 过老,回退快照。
    pub fn replay_view_after(&self, from_seq: u64) -> Option<Vec<ViewUpdate>> {
        self.view_log.replay_after(from_seq)
    }

    /// P6.2: 已提交的最后 seq(快照锚点;无事件时 0)。
    pub fn view_last_seq(&self) -> u64 {
        self.view_log.last_seq()
    }

    fn free_bytes(&self) -> i64 {
        if self.pool_capacity_bytes <= 0 {
            return i64::MAX / 4; // "unlimited" free for borrow
        }
        (self.pool_capacity_bytes - self.total_used_bytes()).max(0)
    }

    /// P4.5: register `(model_id, revision)` namespace.
    ///
    /// Idempotent only when immutable fields match (`num_layers` / `block_spec` /
    /// `hash_algo` / ids). Re-register may update **quota** only; other changes
    /// require a new `revision`.
    pub fn register_model(&mut self, desc: ModelDescriptor) -> Result<(), String> {
        if desc.model_id.is_empty() {
            return Err("RegisterModel: model_id required".into());
        }
        if let Some(q) = desc.quota.as_ref() {
            quota::validate_quota(q).map_err(|e| format!("RegisterModel: {e}"))?;
        }
        let key = NamespaceKey::new(desc.model_id.clone(), desc.revision.clone());
        if let Some(ns) = self.namespaces.get_mut(&key) {
            if !descriptor_identity_eq(&ns.descriptor, &desc) {
                return Err(format!(
                    "RegisterModel: immutable fields changed for ({}, rev={:?}); use a new revision \
                     (num_layers/block_spec/hash_algo must match)",
                    desc.model_id, desc.revision
                ));
            }
            // Same identity → allow quota refresh (same validator as SetModelQuota).
            ns.descriptor.quota = desc.quota;
            return Ok(());
        }
        let cap = self.inactive_cap;
        self.namespaces.insert(key, Namespace::new(desc, cap));
        Ok(())
    }

    /// Cascade-delete one namespace (all pool_kinds). Bytes → P4.7.
    pub fn deregister_model(&mut self, model_id: &str, revision: &str) -> Result<(), String> {
        if model_id.is_empty() {
            return Err("DeregisterModel: model_id required".into());
        }
        let key = NamespaceKey::new(model_id, revision);
        let Some(ns) = self.namespaces.remove(&key) else {
            return Err(format!(
                "DeregisterModel: unknown namespace ({model_id}, rev={revision:?})"
            ));
        };
        // P6.2: 命名空间整体下线 → 其全部 block 对镜像失效。
        for (pk, pool) in &ns.pools {
            let evs = invalidated_events_for(
                model_id,
                revision,
                pool.by_flat
                    .iter()
                    .map(|(flat, entry)| (*pk, entry.seq_hash, flat.clone()))
                    .collect(),
            );
            self.pending_view_events.extend(evs);
        }
        Ok(())
    }

    /// P4.6: update soft/hard/borrow for an existing namespace.
    pub fn set_model_quota(
        &mut self,
        model_id: &str,
        revision: &str,
        quota: Quota,
    ) -> Result<(), String> {
        if model_id.is_empty() {
            return Err("SetModelQuota: model_id required".into());
        }
        quota::validate_quota(&quota).map_err(|e| format!("SetModelQuota: {e}"))?;
        let ns = self.ns_mut(model_id, revision).ok_or_else(|| {
            format!("SetModelQuota: unknown namespace ({model_id}, rev={revision:?})")
        })?;
        ns.descriptor.quota = Some(quota);
        Ok(())
    }

    /// P4.6: snapshot quota + usage (+ backpressure if currently over hard).
    pub fn get_model_quota(
        &self,
        model_id: &str,
        revision: &str,
    ) -> Result<GetModelQuotaResponse, String> {
        if model_id.is_empty() {
            return Err("GetModelQuota: model_id required".into());
        }
        let ns = self.ns(model_id, revision).ok_or_else(|| {
            format!("GetModelQuota: unknown namespace ({model_id}, rev={revision:?})")
        })?;
        let q = ns.quota();
        let used = ns.used_bytes;
        let borrowed = quota::borrowed_bytes(used, q.soft_bytes);
        let backpressure = if q.hard_bytes > 0 && used > q.hard_bytes {
            Some(BackpressureSignal {
                model_id: model_id.into(),
                revision: revision.into(),
                used_bytes: used,
                soft_bytes: q.soft_bytes,
                hard_bytes: q.hard_bytes,
                deficit_bytes: used - q.hard_bytes,
                reason: "HARD_QUOTA".into(),
            })
        } else {
            None
        };
        Ok(GetModelQuotaResponse {
            quota: Some(q),
            used_bytes: used,
            borrowed_bytes: borrowed,
            backpressure,
            ok: true,
            err: String::new(),
        })
    }

    pub fn used_bytes(&self, model_id: &str, revision: &str) -> i64 {
        self.ns(model_id, revision)
            .map(|n| n.used_bytes)
            .unwrap_or(0)
    }

    pub fn has_namespace(&self, model_id: &str, revision: &str) -> bool {
        self.namespaces
            .contains_key(&NamespaceKey::new(model_id, revision))
    }

    pub fn model_descriptor(&self, model_id: &str, revision: &str) -> Option<&ModelDescriptor> {
        self.namespaces
            .get(&NamespaceKey::new(model_id, revision))
            .map(|n| &n.descriptor)
    }

    pub fn block_count(&self, model_id: &str, revision: &str) -> usize {
        self.ns(model_id, revision)
            .map(|n| n.block_count())
            .unwrap_or(0)
    }

    pub(crate) fn ns_mut(&mut self, model_id: &str, revision: &str) -> Option<&mut Namespace> {
        self.namespaces
            .get_mut(&NamespaceKey::new(model_id, revision))
    }

    pub(crate) fn ns(&self, model_id: &str, revision: &str) -> Option<&Namespace> {
        self.namespaces.get(&NamespaceKey::new(model_id, revision))
    }

    /// Register durable blocks. One `RegisterBlocks` batch = one `pool_kind`
    /// contiguous segment of `prefix_hashes`.
    ///
    /// P4.6: charges per-namespace quota. New flats only. Soft overflow → own
    /// inactive eviction + optional borrow/reclaim; hard overflow →
    /// [`RegisterStatus::RejectedHardQuota`] **without** applying eviction/reclaim
    /// (plan then commit; see [`Self::preflight_register`]).
    pub fn register(
        &mut self,
        node_id: &str,
        prefix_hashes: &[Vec<u8>],
        metas: Vec<BlockMeta>,
    ) -> Result<RegisterStatus, String> {
        if metas.is_empty() {
            return Ok(RegisterStatus::Accepted);
        }
        let model_id = metas
            .iter()
            .find_map(|m| m.id.as_ref().map(|i| i.model_id.clone()))
            .ok_or_else(|| "RegisterBlocks: no KVBlockID".to_string())?;
        let revision = metas
            .iter()
            .find_map(|m| m.id.as_ref().map(|i| i.revision.clone()))
            .unwrap_or_default();
        let pool_kind = {
            let raw = metas
                .iter()
                .find_map(|m| m.id.as_ref().map(|i| i.pool_kind))
                .unwrap_or(PoolKind::Target as i32);
            resolve_pool_kind(raw)?
        };

        if prefix_hashes.is_empty() {
            return Err("RegisterBlocks: prefix_hashes required (P4.2 lineage)".into());
        }

        let index_of: HashMap<&[u8], usize> = prefix_hashes
            .iter()
            .enumerate()
            .map(|(i, h)| (h.as_slice(), i))
            .collect();

        let mut positions: Vec<usize> = Vec::with_capacity(metas.len());
        for meta in &metas {
            let Some(id) = meta.id.as_ref() else {
                continue;
            };
            if id.model_id != model_id {
                return Err(format!(
                    "RegisterBlocks: mixed model_id {} vs {}",
                    model_id, id.model_id
                ));
            }
            if id.revision != revision {
                return Err(format!(
                    "RegisterBlocks: mixed revision {:?} vs {:?}",
                    revision, id.revision
                ));
            }
            let pk = resolve_pool_kind(id.pool_kind)?;
            if pk != pool_kind {
                return Err(format!(
                    "RegisterBlocks: mixed pool_kind {pk} vs {pool_kind}"
                ));
            }
            let Some(&pos) = index_of.get(id.block_hash.as_slice()) else {
                return Err(format!(
                    "RegisterBlocks: block hash not in prefix_hashes (len={})",
                    id.block_hash.len()
                ));
            };
            positions.push(pos);
        }
        if positions.is_empty() {
            return Err("RegisterBlocks: no KVBlockID".into());
        }
        positions.sort_unstable();
        positions.dedup();
        for w in positions.windows(2) {
            if w[1] != w[0] + 1 {
                return Err(
                    "RegisterBlocks: blocks must be a contiguous segment of prefix_hashes".into(),
                );
            }
        }

        if !self.has_namespace(&model_id, &revision) {
            return Err(format!(
                "RegisterBlocks: model not registered ({model_id}, rev={revision:?}); call RegisterModel first"
            ));
        }

        // Count new unique flats (idempotent re-register of existing = 0 charge).
        let new_block_count = {
            let ns = self.ns(&model_id, &revision).expect("checked");
            let pool = ns.pool(pool_kind);
            let mut seen = std::collections::HashSet::new();
            let mut n = 0usize;
            for meta in &metas {
                let Some(id) = meta.id.as_ref() else { continue };
                if !seen.insert(id.block_hash.clone()) {
                    continue;
                }
                let exists = pool
                    .map(|p| p.by_flat.contains_key(&id.block_hash))
                    .unwrap_or(false);
                if !exists {
                    n += 1;
                }
            }
            n
        };

        match self.admit_register_bytes(&model_id, &revision, new_block_count)? {
            RegisterStatus::Accepted => {}
            rejected => return Ok(rejected),
        }

        // Validate-then-mutate(P6.2):耐久性校验前置,变异循环不再中途失败——
        // 视图事件必须恰好对应已提交的状态迁移。
        for meta in &metas {
            if meta.id.is_some() && meta.locations.is_empty() && !meta.l3_present {
                return Err(
                    "RegisterBlocks: need L2 location or l3_present (durable first)".into(),
                );
            }
        }

        let lineage = lineage_from_prefix(prefix_hashes);
        let mut clear_orphans = Vec::new();
        let mut view_events = Vec::new();
        {
            let ns = self
                .ns_mut(&model_id, &revision)
                .expect("checked has_namespace");
            let bpb = ns.bytes_per_block();
            let pool = ns.pool_mut(pool_kind);
            let mut charged = 0i64;

            for mut meta in metas {
                let Some(id) = meta.id.clone() else { continue };
                // Normalize unspecified → TARGET on stored identity.
                if let Some(ref mut mid) = meta.id {
                    mid.pool_kind = pool_kind;
                }
                let flat = id.block_hash.clone();
                let pos = *index_of.get(flat.as_slice()).expect("checked");
                let seq = lineage[pos];

                for loc in &mut meta.locations {
                    if loc.node_id.is_empty() {
                        loc.node_id = node_id.to_string();
                    }
                }

                let is_new = !pool.by_flat.contains_key(&flat);

                let handle = pool.registry.register_sequence_hash(seq);
                for loc in &meta.locations {
                    if loc.tier == Tier::L0 as i32 {
                        handle.mark_present::<TierL0>();
                    } else if loc.tier == Tier::L1 as i32 {
                        handle.mark_present::<TierL1>();
                    } else if loc.tier == Tier::L2 as i32 {
                        handle.mark_present::<TierL2>();
                    }
                }
                pool.handles.insert(seq, handle);

                let block_id = if let Some(prev) = pool.by_flat.get(&flat) {
                    prev.block_id
                } else {
                    pool.alloc_block_id()
                };
                pool.seq_to_flat.insert(seq, flat.clone());
                let prefix_chain = prefix_hashes[..=pos].to_vec();
                // P6.2: 事件携带变更后全量位置;is_new → REGISTERED,否则 MOVED。
                view_events.push(crate::view::upsert_event(
                    if is_new {
                        view_event::Kind::Registered
                    } else {
                        view_event::Kind::Moved
                    },
                    meta.id.clone().expect("checked"),
                    meta.locations.clone(),
                    meta.l3_present,
                    meta.block_kind,
                ));
                pool.by_flat.insert(
                    flat.clone(),
                    Entry {
                        seq_hash: seq,
                        meta,
                        block_id,
                        prefix_chain,
                    },
                );
                // Successful PutEnd clears any stale orphan mark for this flat.
                clear_orphans.push(crate::reconcile::BlockKey {
                    model_id: model_id.clone(),
                    revision: revision.clone(),
                    pool_kind,
                    flat,
                });
                if is_new {
                    charged += bpb;
                }
            }
            ns.used_bytes = ns.used_bytes.saturating_add(charged);
        }
        self.pending_view_events.extend(view_events);
        for k in clear_orphans {
            self.orphans.remove(&k);
        }
        Ok(RegisterStatus::Accepted)
    }

    /// Read-only quota preflight (Mooncake PutStart-shaped). No eviction/reclaim.
    ///
    /// PutEnd should call this **before** `flush_durable` so hard reject does not
    /// leave orphan tier bytes. [`Self::register`] re-runs plan+commit under the
    /// same rules (single-process; reserved charge deferred).
    pub fn preflight_register(
        &self,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
        block_hashes: &[Vec<u8>],
    ) -> Result<RegisterStatus, String> {
        let pk = resolve_pool_kind(pool_kind)?;
        if !self.has_namespace(model_id, revision) {
            return Err(format!(
                "preflight: model not registered ({model_id}, rev={revision:?})"
            ));
        }
        let new_blocks = self.count_new_flats(model_id, revision, pk, block_hashes);
        let plan = self.plan_admit(model_id, revision, new_blocks)?;
        Ok(plan.status())
    }

    fn count_new_flats(
        &self,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
        block_hashes: &[Vec<u8>],
    ) -> usize {
        let Some(ns) = self.ns(model_id, revision) else {
            return block_hashes.len();
        };
        let pool = ns.pool(pool_kind);
        let mut seen = std::collections::HashSet::new();
        let mut n = 0usize;
        for h in block_hashes {
            if !seen.insert(h.as_slice()) {
                continue;
            }
            let exists = pool.map(|p| p.by_flat.contains_key(h)).unwrap_or(false);
            if !exists {
                n += 1;
            }
        }
        n
    }

    /// Plan+commit admission. Hard reject never mutates (no eviction/reclaim).
    fn admit_register_bytes(
        &mut self,
        model_id: &str,
        revision: &str,
        new_blocks: usize,
    ) -> Result<RegisterStatus, String> {
        let plan = self.plan_admit(model_id, revision, new_blocks)?;
        if let AdmitPlan::Reject(bp) = &plan {
            return Ok(RegisterStatus::RejectedHardQuota(bp.clone()));
        }
        self.commit_admit(model_id, revision, &plan);
        Ok(RegisterStatus::Accepted)
    }

    /// Pure planning: decide reject vs how many own/other victims to free.
    fn plan_admit(
        &self,
        model_id: &str,
        revision: &str,
        new_blocks: usize,
    ) -> Result<AdmitPlan, String> {
        if new_blocks == 0 {
            return Ok(AdmitPlan::Accept {
                own_evict_n: 0,
                reclaim_bytes: 0,
            });
        }
        let key = NamespaceKey::new(model_id, revision);
        let ns = self
            .ns(model_id, revision)
            .ok_or_else(|| format!("admit: unknown namespace ({model_id}, rev={revision:?})"))?;
        let q = ns.quota();
        let bpb = ns.bytes_per_block();
        let used = ns.used_bytes;
        let delta = (new_blocks as i64).saturating_mul(bpb);
        let inactive_n = ns.inactive_len();
        let inactive_bytes = ns.inactive_bytes();

        // Hard ceiling: if even max own inactive eviction cannot fit, reject
        // with **zero** mutation (review #1).
        if q.hard_bytes > 0 && used.saturating_add(delta) > q.hard_bytes {
            let need = used.saturating_add(delta) - q.hard_bytes;
            if inactive_bytes < need {
                let bp = AdmitWrite::HardQuota {
                    used_bytes: used,
                    soft_bytes: q.soft_bytes,
                    hard_bytes: q.hard_bytes,
                    deficit_bytes: need - inactive_bytes,
                }
                .backpressure(model_id, revision)
                .expect("HardQuota");
                return Ok(AdmitPlan::Reject(bp));
            }
        }

        // Own inactive eviction: enough for hard fit + soft pressure.
        let mut own_evict_n = 0usize;
        if q.hard_bytes > 0 && used.saturating_add(delta) > q.hard_bytes {
            let need = used.saturating_add(delta) - q.hard_bytes;
            own_evict_n = own_evict_n.max(((need + bpb - 1) / bpb) as usize);
        }
        if q.soft_bytes > 0 && used.saturating_add(delta) > q.soft_bytes {
            let need = used.saturating_add(delta) - q.soft_bytes;
            own_evict_n = own_evict_n.max(((need + bpb - 1) / bpb) as usize);
        }
        own_evict_n = own_evict_n.min(inactive_n);

        let sim_used = used.saturating_sub((own_evict_n as i64).saturating_mul(bpb));
        let mut reclaim_bytes = 0i64;

        if q.borrow_enabled && q.soft_bytes > 0 {
            let projected = sim_used.saturating_add(delta);
            if projected > q.soft_bytes {
                let need_borrow = projected - q.soft_bytes;
                // Own eviction frees pool capacity too when capacity is finite.
                let free = if self.pool_capacity_bytes > 0 {
                    (self.free_bytes() + (own_evict_n as i64).saturating_mul(bpb)).max(0)
                } else {
                    self.free_bytes()
                };
                if free < need_borrow {
                    let want = need_borrow - free;
                    let avail = self.reclaimable_borrowed_bytes(&key);
                    if avail < want
                        && self.pool_capacity_bytes > 0
                        && (q.hard_bytes == 0 || projected <= q.hard_bytes)
                    {
                        let bp = AdmitWrite::PoolCapacity {
                            used_bytes: projected,
                            soft_bytes: q.soft_bytes,
                            hard_bytes: q.hard_bytes,
                            deficit_bytes: want - avail,
                        }
                        .backpressure(model_id, revision)
                        .expect("PoolCapacity");
                        return Ok(AdmitPlan::Reject(bp));
                    }
                    reclaim_bytes = want.min(avail);
                }
            }
        }

        match quota::classify_write(sim_used, delta, &q) {
            r @ AdmitWrite::HardQuota { .. } => {
                let bp = r.backpressure(model_id, revision).expect("HardQuota");
                Ok(AdmitPlan::Reject(bp))
            }
            AdmitWrite::PoolCapacity { .. } => {
                unreachable!("classify_write does not produce PoolCapacity")
            }
            AdmitWrite::WithinSoft | AdmitWrite::OverSoft => Ok(AdmitPlan::Accept {
                own_evict_n,
                reclaim_bytes,
            }),
        }
    }

    fn commit_admit(&mut self, model_id: &str, revision: &str, plan: &AdmitPlan) {
        let AdmitPlan::Accept {
            own_evict_n,
            reclaim_bytes,
        } = plan
        else {
            return;
        };
        if *own_evict_n > 0 {
            let removed = if let Some(ns) = self.ns_mut(model_id, revision) {
                ns.evict_inactive_n(*own_evict_n)
            } else {
                Vec::new()
            };
            self.pending_view_events
                .extend(invalidated_events_for(model_id, revision, removed));
        }
        if *reclaim_bytes > 0 {
            let key = NamespaceKey::new(model_id, revision);
            self.reclaim_borrowed_bytes(&key, *reclaim_bytes);
        }
    }

    /// Read-only: how many borrowed inactive bytes other namespaces can yield.
    fn reclaimable_borrowed_bytes(&self, except: &NamespaceKey) -> i64 {
        let mut total = 0i64;
        for (k, ns) in &self.namespaces {
            if k == except {
                continue;
            }
            let over = quota::borrowed_bytes(ns.used_bytes, ns.quota().soft_bytes);
            if over <= 0 {
                continue;
            }
            total = total.saturating_add(over.min(ns.inactive_bytes()));
        }
        total
    }

    /// Evict inactive blocks from other namespaces that are over soft (borrowers).
    fn reclaim_borrowed_bytes(&mut self, except: &NamespaceKey, want_bytes: i64) -> i64 {
        if want_bytes <= 0 {
            return 0;
        }
        let mut freed = 0i64;
        let mut keys: Vec<NamespaceKey> = self
            .namespaces
            .iter()
            .filter(|(k, ns)| {
                *k != except && quota::borrowed_bytes(ns.used_bytes, ns.quota().soft_bytes) > 0
            })
            .map(|(k, _)| k.clone())
            .collect();
        keys.sort_by(|a, b| {
            a.model_id
                .cmp(&b.model_id)
                .then(a.revision.cmp(&b.revision))
        });

        for key in keys {
            if freed >= want_bytes {
                break;
            }
            let Some(ns) = self.namespaces.get_mut(&key) else {
                continue;
            };
            let soft = ns.quota().soft_bytes;
            let bpb = ns.bytes_per_block();
            let over = quota::borrowed_bytes(ns.used_bytes, soft);
            if over <= 0 {
                continue;
            }
            let still = want_bytes - freed;
            let target = still.min(over);
            let want_blocks = ((target + bpb - 1) / bpb).max(1) as usize;
            let removed = ns.evict_inactive_n(want_blocks);
            freed += (removed.len() as i64) * bpb;
            let evs = invalidated_events_for(&key.model_id, &key.revision, removed);
            self.pending_view_events.extend(evs);
        }
        freed
    }

    /// Prefix lookup in one `pool_kind` domain.
    ///
    /// `pool_kind == UNSPECIFIED` → TARGET (P4.5 default; draft prefix opt-in).
    ///
    /// Read-only (`&self`, P6.1): no structural repair on the read path —
    /// handles are created eagerly by `register` / `import_snapshot` (the only
    /// `by_flat` ingestion paths), so a missing handle here is defensive-only
    /// and simply skips the touch. Frequency touch stays on the lookup hot
    /// path via `BlockRegistry::touch` (internally synchronized, `&self`),
    /// preserving query-hotness TinyLFU semantics: a D-direct lookup queries
    /// without ever taking a ref, so moving the touch to `report_ref` would
    /// silently change eviction ordering.
    pub fn lookup_prefix(
        &self,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
        prefix_hashes: &[Vec<u8>],
        requester: &str,
    ) -> (Vec<ReusableBlock>, u32, bool) {
        if prefix_hashes.is_empty() {
            return (Vec::new(), 0, false);
        }
        let Ok(pool_kind) = resolve_pool_kind(pool_kind) else {
            return (Vec::new(), 0, false);
        };
        if self.ns(model_id, revision).is_none() {
            return (Vec::new(), 0, false);
        }
        let lineage = lineage_from_prefix(prefix_hashes);
        let ns = self.ns(model_id, revision).expect("checked");
        let Some(pool) = ns.pools.get(&pool_kind) else {
            return (Vec::new(), 0, false);
        };

        let mut out = Vec::new();
        let mut hit = 0u32;
        let mut all_local = true;

        for (i, flat) in prefix_hashes.iter().enumerate() {
            let seq = lineage[i];
            let Some(entry) = pool.by_flat.get(flat) else {
                all_local = false;
                break;
            };
            if entry.seq_hash != seq {
                all_local = false;
                break;
            }
            // S5(issue #74,评估后保留):此处 clone BlockMeta 是有意为之。
            // 评估结论:① proto 边界(ReusableBlock.meta / locate 的 Vec<BlockMeta>)
            // 要求 owned 值,内部改 Arc<BlockMeta> 只是把 clone 挪到边界、省不掉;
            // ② BlockMeta 体量小(id + 少量 locations + 时间戳),clone 成本 O(locations);
            // ③ lookup_prefix 每请求只 clone 命中前缀段(受前缀长度约束),locate 走
            // 批量/观测路径而非逐 token 热路径。若 P7  profiling 证明此处成热点的,
            // 再考虑返回轻量视图(hash+locations 摘要)并同步改 proto。
            let meta = entry.meta.clone();
            if pool.handles.contains_key(&seq) {
                pool.registry.touch(seq);
            }

            let local = meta
                .locations
                .iter()
                .any(|l| l.tier == Tier::L0 as i32 && l.node_id == requester);
            if !local {
                all_local = false;
            }
            out.push(ReusableBlock {
                id: meta.id.clone(),
                meta: Some(meta),
                local_hit: local,
            });
            hit += 1;
        }
        if hit == 0 {
            all_local = false;
        }
        (out, hit, all_local && hit > 0)
    }

    pub fn locate(&self, ids: &[KvBlockId]) -> Vec<BlockMeta> {
        let mut blocks = Vec::new();
        for id in ids {
            let Ok(pk) = resolve_pool_kind(id.pool_kind) else {
                continue;
            };
            if let Some(ns) = self.ns(&id.model_id, &id.revision) {
                if let Some(pool) = ns.pool(pk) {
                    if let Some(entry) = pool.by_flat.get(&id.block_hash) {
                        blocks.push(entry.meta.clone());
                    }
                }
            }
        }
        blocks
    }

    pub fn check_report_ref(&self, delta: &RefDelta) -> Result<(), String> {
        self.ref_target(delta).map(|_| ())
    }

    pub(crate) fn ref_target(&self, delta: &RefDelta) -> Result<RefTarget, String> {
        let id = delta
            .id
            .as_ref()
            .ok_or_else(|| "RefDelta missing id".to_string())?;
        let pool_kind = resolve_pool_kind(id.pool_kind)?;
        let key = NamespaceKey::from_id(id);
        let ns = self
            .namespaces
            .get(&key)
            .ok_or_else(|| format!("unknown namespace ({}, rev={:?})", id.model_id, id.revision))?;
        let pool = ns
            .pool(pool_kind)
            .ok_or_else(|| format!("RefDelta: unknown pool_kind {pool_kind}"))?;
        let entry = pool
            .by_flat
            .get(&id.block_hash)
            .ok_or_else(|| "RefDelta: unknown block_hash".to_string())?;
        Ok(RefTarget {
            key,
            pool_kind,
            seq: entry.seq_hash,
        })
    }

    pub fn report_ref(&mut self, delta: &RefDelta) -> Result<(), String> {
        self.report_ref_raw(delta, /*track_node*/ true)
    }

    pub fn report_refs(&mut self, deltas: &[RefDelta]) -> Result<(), String> {
        // S4:批投影按 kind 分账——同一批内同块不同 kind 各自累计、各自校验。
        let mut projected: HashMap<RefTarget, RefAccounts> = HashMap::new();
        let mut projected_node: HashMap<(String, crate::reconcile::BlockKey), RefAccounts> =
            HashMap::new();
        for (i, d) in deltas.iter().enumerate() {
            let target = self
                .ref_target(d)
                .map_err(|e| format!("ReportRef batch[{i}]: {e}"))?;
            let kind =
                resolve_ref_kind(d.kind).map_err(|e| format!("ReportRef batch[{i}]: {e}"))?;
            let id = d.id.as_ref().expect("ref_target checked id");
            let accounts = projected.entry(target.clone()).or_insert_with(|| {
                self.namespaces
                    .get(&target.key)
                    .and_then(|ns| ns.pool(target.pool_kind))
                    .and_then(|pool| pool.global_refs.get(&target.seq).copied())
                    .unwrap_or_default()
            });
            let bucket = accounts.bucket_mut(kind);
            let next = bucket.checked_add(i64::from(d.delta)).ok_or_else(|| {
                format!(
                    "ReportRef batch[{i}]: ref_count overflow (kind={})",
                    kind.as_str_name()
                )
            })?;
            if next < 0 {
                return Err(format!(
                    "ReportRef batch[{i}]: ref_count underflow (kind={})",
                    kind.as_str_name()
                ));
            }
            *bucket = next;

            if !d.node_id.is_empty() && d.delta != 0 {
                let block_key = crate::reconcile::BlockKey::from_id(id);
                let node_key = (d.node_id.clone(), block_key);
                let accounts = projected_node.entry(node_key.clone()).or_insert_with(|| {
                    self.node_refs
                        .get(&node_key.0)
                        .and_then(|held| held.get(&node_key.1).copied())
                        .unwrap_or_default()
                });
                let bucket = accounts.bucket_mut(kind);
                let next = bucket.checked_add(i64::from(d.delta)).ok_or_else(|| {
                    format!(
                        "ReportRef batch[{i}]: node_ref overflow (kind={})",
                        kind.as_str_name()
                    )
                })?;
                if next < 0 {
                    return Err(format!(
                        "ReportRef batch[{i}]: node_ref underflow (kind={})",
                        kind.as_str_name()
                    ));
                }
                *bucket = next;
            }
        }
        for d in deltas {
            self.report_ref(d)
                .expect("report_ref after successful check_report_ref");
        }
        Ok(())
    }

    pub fn evict_n(&mut self, model_id: &str, revision: &str, pool_kind: i32, n: usize) -> usize {
        let Ok(pk) = resolve_pool_kind(pool_kind) else {
            return 0;
        };
        let Some(ns) = self.ns_mut(model_id, revision) else {
            return 0;
        };
        let bpb = ns.bytes_per_block();
        let removed = {
            let Some(pool) = ns.pools.get_mut(&pk) else {
                return 0;
            };
            pool.drop_inactive_victims(n)
        };
        ns.used_bytes = (ns.used_bytes - (removed.len() as i64) * bpb).max(0);
        let count = removed.len();
        let evs = invalidated_events_for(
            model_id,
            revision,
            removed.into_iter().map(|(s, f)| (pk, s, f)).collect(),
        );
        self.pending_view_events.extend(evs);
        count
    }

    pub fn inactive_len(&self, model_id: &str, revision: &str, pool_kind: i32) -> usize {
        let Ok(pk) = resolve_pool_kind(pool_kind) else {
            return 0;
        };
        self.ns(model_id, revision)
            .and_then(|n| n.pool(pk))
            .map(|p| p.inactive.len())
            .unwrap_or(0)
    }

    /// 总 ref(两种 kind 合计)——驱逐冻结语义的判定口径。
    pub fn global_ref(&self, model_id: &str, revision: &str, pool_kind: i32, flat: &[u8]) -> i64 {
        let (request, writeback) = self.global_ref_by_kind(model_id, revision, pool_kind, flat);
        request + writeback
    }

    /// S4(issue #74):分 kind 观测——返回 `(request, writeback)`。
    pub fn global_ref_by_kind(
        &self,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
        flat: &[u8],
    ) -> (i64, i64) {
        let Ok(pk) = resolve_pool_kind(pool_kind) else {
            return (0, 0);
        };
        let Some(ns) = self.ns(model_id, revision) else {
            return (0, 0);
        };
        let Some(pool) = ns.pool(pk) else {
            return (0, 0);
        };
        let Some(entry) = pool.by_flat.get(flat) else {
            return (0, 0);
        };
        let a = pool
            .global_refs
            .get(&entry.seq_hash)
            .copied()
            .unwrap_or_default();
        (a.request, a.writeback)
    }

    pub fn complete_barrier(&mut self, request_id: &str, node_id: &str) -> Result<(), String> {
        if request_id.is_empty() {
            return Err("RequestBarrier: request_id required".into());
        }
        if node_id.is_empty() {
            return Err("RequestBarrier: node_id required".into());
        }
        self.completed_barriers
            .insert(request_id.to_string(), node_id.to_string());
        Ok(())
    }

    pub fn barrier_completed(&self, request_id: &str) -> bool {
        self.completed_barriers.contains_key(request_id)
    }

    #[allow(clippy::too_many_arguments)] // wire-shaped presence update; pack later if needed
    pub fn publish_location(
        &mut self,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
        flat: &[u8],
        tier: Tier,
        node_id: &str,
        present: bool,
    ) -> Result<(), String> {
        // Legacy default placement (pre-P4.8 callers).
        self.publish_location_at(
            model_id, revision, pool_kind, flat, tier, node_id, 1, 0, present,
        )
    }

    /// Publish presence with explicit segment/offset (P4.8).
    #[allow(clippy::too_many_arguments)] // wire-shaped presence + placement coords
    pub fn publish_location_at(
        &mut self,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
        flat: &[u8],
        tier: Tier,
        node_id: &str,
        segment_id: u64,
        offset: u64,
        present: bool,
    ) -> Result<(), String> {
        let pk = resolve_pool_kind(pool_kind)?;
        let ns = self
            .ns_mut(model_id, revision)
            .ok_or_else(|| format!("unknown namespace ({model_id}, rev={revision:?})"))?;
        let pool = ns
            .pools
            .get_mut(&pk)
            .ok_or_else(|| format!("publish_location: unknown pool_kind {pk}"))?;
        let entry = pool
            .by_flat
            .get_mut(flat)
            .ok_or_else(|| "publish_location: unknown block".to_string())?;
        let handle = pool
            .handles
            .get(&entry.seq_hash)
            .ok_or_else(|| "publish_location: missing handle".to_string())?;

        let tier_i = tier as i32;
        let had = entry
            .meta
            .locations
            .iter()
            .any(|l| l.tier == tier_i && l.node_id == node_id);

        if present && !had {
            entry.meta.locations.push(Location {
                tier: tier_i,
                node_id: node_id.to_string(),
                segment_id,
                offset,
            });
            match tier {
                Tier::L0 => handle.mark_present::<TierL0>(),
                Tier::L1 => handle.mark_present::<TierL1>(),
                Tier::L2 => handle.mark_present::<TierL2>(),
                _ => {}
            }
        } else if present && had {
            // Refresh coordinates (defrag Moved / re-publish).
            for loc in &mut entry.meta.locations {
                if loc.tier == tier_i && loc.node_id == node_id {
                    loc.segment_id = segment_id;
                    loc.offset = offset;
                    break;
                }
            }
        } else if !present && had {
            entry
                .meta
                .locations
                .retain(|l| !(l.tier == tier_i && l.node_id == node_id));
            match tier {
                Tier::L0 => handle.mark_absent::<TierL0>(),
                Tier::L1 => handle.mark_absent::<TierL1>(),
                Tier::L2 => handle.mark_absent::<TierL2>(),
                _ => {}
            }
            // S1(issue #74):L0 驱逐连带清 (flat,node) 放置滞回标记——否则已计划→
            // 放置→被驱逐的块永远无法再触发对该节点的放置,follow_traffic 注释声称的
            // 「驱逐后可再触发(自愈)」不成立。与死节点清 marks(reconcile.rs
            // `placement_marks.retain(|(_, n)| ...)`)同构。
            if tier == Tier::L0 {
                pool.placement_marks
                    .remove(&(flat.to_vec(), node_id.to_string()));
            }
        }
        // P6.2: 任何实际位置变更 → MOVED(变更后全量位置);!present && !had 为无操作。
        let view_ev = if present || had {
            Some(crate::view::upsert_event(
                view_event::Kind::Moved,
                entry.meta.id.clone().unwrap_or_else(|| KvBlockId {
                    model_id: model_id.into(),
                    revision: revision.into(),
                    pool_kind: pk,
                    block_hash: flat.to_vec(),
                    scope: "public".into(),
                }),
                entry.meta.locations.clone(),
                entry.meta.l3_present,
                0,
            ))
        } else {
            None
        };
        if let Some(ev) = view_ev {
            tracing::debug!(
                model_id,
                revision,
                pool_kind,
                tier = ?tier,
                node_id,
                present,
                "publish_location"
            );
            self.pending_view_events.push(ev);
        }
        Ok(())
    }

    pub fn set_l3_present(
        &mut self,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
        flat: &[u8],
        present: bool,
    ) -> Result<(), String> {
        let pk = resolve_pool_kind(pool_kind)?;
        let ns = self
            .ns_mut(model_id, revision)
            .ok_or_else(|| format!("unknown namespace ({model_id}, rev={revision:?})"))?;
        let pool = ns
            .pools
            .get_mut(&pk)
            .ok_or_else(|| format!("set_l3_present: unknown pool_kind {pk}"))?;
        let entry = pool
            .by_flat
            .get_mut(flat)
            .ok_or_else(|| "set_l3_present: unknown block".to_string())?;
        entry.meta.l3_present = present;
        // P6.2: l3 存在性变更 → MOVED(位置不变,全量带上)。
        let view_ev = crate::view::upsert_event(
            view_event::Kind::Moved,
            entry.meta.id.clone().unwrap_or_else(|| KvBlockId {
                model_id: model_id.into(),
                revision: revision.into(),
                pool_kind: pk,
                block_hash: flat.to_vec(),
                scope: "public".into(),
            }),
            entry.meta.locations.clone(),
            entry.meta.l3_present,
            0,
        );
        self.pending_view_events.push(view_ev);
        Ok(())
    }

    pub fn has_l0_on(
        &self,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
        flat: &[u8],
        node_id: &str,
    ) -> bool {
        let Ok(pk) = resolve_pool_kind(pool_kind) else {
            return false;
        };
        self.ns(model_id, revision)
            .and_then(|n| n.pool(pk))
            .and_then(|p| p.by_flat.get(flat))
            .map(|e| {
                e.meta
                    .locations
                    .iter()
                    .any(|l| l.tier == Tier::L0 as i32 && l.node_id == node_id)
            })
            .unwrap_or(false)
    }

    /// Read the refcounted L0 presence-marker shadow for a block.
    /// Unlike `has_l0_on` (which reads the authoritative `locations` vec),
    /// this reads the registry's refcounted shadow that `mark_present`/
    /// `mark_absent` maintain. The two must agree in steady state; a
    /// divergence indicates a leaked/decrement-skipped marker. Exposed for
    /// reconcile/eviction correctness tests (and future `&self lookup_prefix`
    /// via `check_presence`).
    pub fn has_l0_presence(
        &self,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
        flat: &[u8],
    ) -> bool {
        let Ok(pk) = resolve_pool_kind(pool_kind) else {
            return false;
        };
        self.ns(model_id, revision)
            .and_then(|n| n.pool(pk))
            .and_then(|p| p.by_flat.get(flat).and_then(|e| p.handles.get(&e.seq_hash)))
            .map(|h| h.has_block::<TierL0>())
            .unwrap_or(false)
    }
}
