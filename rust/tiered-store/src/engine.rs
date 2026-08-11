//! 本地分层字节引擎：L0(HBM 站位) / L1(DRAM) / L2(NVMe) / L3(对象存储站位)。
//!
//! 参考：SGLang `hiradix_cache.py::_evict_write_back`（`lock_ref>0` 跳过）+
//! `write_backup` 后再丢 device；Dynamo offload 副作用需回写 presence。
//! 关键差异：无 BlockStore；位置权威在 CP；本 crate 通过 [`TierSideEffects`] 上报副作用。

use std::collections::{HashMap, HashSet, VecDeque};

use crate::segment::{Placement, Relocate, SegmentArena, DEFAULT_SLOT_BYTES};
use crate::stats::{AccessKind, HitStats};

/// Soft capacity knobs (block counts). `0` = unlimited.
#[derive(Debug, Clone, Copy)]
pub struct TierCaps {
    pub l0: usize,
    pub l1: usize,
    pub l2: usize,
}

impl Default for TierCaps {
    fn default() -> Self {
        Self {
            l0: 64,
            l1: 256,
            l2: 1024,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LocalTier {
    L0,
    L1,
    L2,
    L3,
}

/// [`LocalTierEngine::begin_promote`] 登记结果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BeginPromote {
    /// 新登记在途,字节未搬。
    Started,
    /// 同块已在途(去重命中,不重复发起)。
    AlreadyInFlight,
    /// 已在 L0,无需 promote。
    AlreadyL0,
    /// 各层均无此块。
    Missing,
}

/// Collateral tier mutations that **must** be published to the controlplane view.
///
/// Mirrors Dynamo transfer→presence apply: engine mutates bytes, caller syncs markers.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct TierSideEffects {
    pub l0_demoted: Vec<Vec<u8>>,
    pub l2_demoted_to_l3: Vec<Vec<u8>>,
}

impl TierSideEffects {
    pub fn merge(&mut self, other: TierSideEffects) {
        self.l0_demoted.extend(other.l0_demoted);
        self.l2_demoted_to_l3.extend(other.l2_demoted_to_l3);
    }

    pub fn is_empty(&self) -> bool {
        self.l0_demoted.is_empty() && self.l2_demoted_to_l3.is_empty()
    }
}

/// In-process L0–L3 byte maps + LRU + local pin (≈ SGLang `lock_ref`).
pub struct LocalTierEngine {
    caps: TierCaps,
    l0: HashMap<Vec<u8>, Vec<u8>>,
    l1: HashMap<Vec<u8>, Vec<u8>>,
    l2: HashMap<Vec<u8>, Vec<u8>>,
    l3: HashMap<Vec<u8>, Vec<u8>>,
    l0_order: VecDeque<Vec<u8>>,
    l1_order: VecDeque<Vec<u8>>,
    l2_order: VecDeque<Vec<u8>>,
    /// Local freeze (request / writeback / in-flight). Demote skips when >0.
    pins: HashMap<Vec<u8>, u32>,
    /// 命中计数(决策 B 准入信号)。**原型本地站位**:生产权威挂 CP radix 节点
    /// (SGLang `TreeNode.hit_count` 同款),经位置视图镜像带到 agent;此处引擎
    /// 本地记账,语义对齐 `hiradix_cache.py::_inc_hit_count`。
    hit_counts: HashMap<Vec<u8>, u32>,
    /// promote 频率准入阈值:hit_count ≥ 此值才给热块待遇(默认 2,对齐 SGLang
    /// `write_through_selective` threshold=2;1 = 关闭准入,退化为命中即热)。
    /// per-agent 配置,无需跨节点一致。
    promote_admit_after: u32,
    /// one-shot 标记:冷块(hit_count < 阈值)promote 进 L0 后**驱逐最优先**,
    /// 不挤兑热块(SGLang 只备份热数据 / Mooncake CountMinSketch 准入的同族形态;
    /// 注意 GPU 约束下"不加载直读"不成立,冷块仍须进 L0 才能算,准入砍的是级联)。
    one_shot: HashSet<Vec<u8>>,
    /// promote 在途登记(决策:in-flight 去重)。同块重复发起只登记一次,
    /// 对齐 Dynamo offload 去重;异步原语见 [`begin_promote`] / [`finish_promote`]。
    promote_inflight: HashSet<Vec<u8>>,
    /// L2 segment layout (P4.8); bytes stay in `l2`, arena tracks segment/offset.
    pub l2_arena: SegmentArena,
    pub stats: HitStats,
}

impl Default for LocalTierEngine {
    fn default() -> Self {
        Self::with_caps(TierCaps::default())
    }
}

impl LocalTierEngine {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_caps(caps: TierCaps) -> Self {
        Self::with_caps_arena(caps, SegmentArena::new(DEFAULT_SLOT_BYTES, 64))
    }

    pub fn with_caps_arena(caps: TierCaps, l2_arena: SegmentArena) -> Self {
        Self {
            caps,
            l0: HashMap::new(),
            l1: HashMap::new(),
            l2: HashMap::new(),
            l3: HashMap::new(),
            l0_order: VecDeque::new(),
            l1_order: VecDeque::new(),
            l2_order: VecDeque::new(),
            pins: HashMap::new(),
            hit_counts: HashMap::new(),
            promote_admit_after: 2,
            one_shot: HashSet::new(),
            promote_inflight: HashSet::new(),
            l2_arena,
            stats: HitStats::default(),
        }
    }

    /// per-agent 准入阈值配置(1 = 关闭准入)。
    pub fn with_promote_admit_after(mut self, n: u32) -> Self {
        self.promote_admit_after = n.max(1);
        self
    }

    pub fn hit_count(&self, h: &[u8]) -> u32 {
        self.hit_counts.get(h).copied().unwrap_or(0)
    }

    pub fn is_one_shot(&self, h: &[u8]) -> bool {
        self.one_shot.contains(h)
    }

    pub fn l2_placement(&self, h: &[u8]) -> Option<Placement> {
        self.l2_arena.placement(h)
    }

    fn note_l2_present(&mut self, h: &[u8]) {
        if self.l2_arena.placement(h).is_none() {
            let _ = self.l2_arena.alloc(h);
        }
    }

    fn note_l2_absent(&mut self, h: &[u8]) {
        let _ = self.l2_arena.free(h);
    }

    /// Compact one L2 segment. Fails if any resident block is pinned.
    pub fn compact_l2_segment(&mut self, segment_id: u64) -> Result<Vec<Relocate>, String> {
        for hash in self.l2_arena.hashes_in_segment(segment_id) {
            if self.pin_count(&hash) > 0 {
                return Err("compact: pinned block in segment".into());
            }
        }
        self.l2_arena.compact(segment_id)
    }

    /// Co-locate move on L2 arena. Fails if pinned or hash not on L2.
    pub fn colocate_l2_move(
        &mut self,
        hash: &[u8],
        dest_segment: u64,
        dest_offset: u64,
    ) -> Result<Relocate, String> {
        if self.pin_count(hash) > 0 {
            return Err("colocate: pinned".into());
        }
        if !self.l2.contains_key(hash) {
            return Err("colocate: not on L2".into());
        }
        self.l2_arena.relocate(hash, dest_segment, dest_offset)
    }

    pub fn caps(&self) -> TierCaps {
        self.caps
    }

    pub fn l0_len(&self) -> usize {
        self.l0.len()
    }
    pub fn l1_len(&self) -> usize {
        self.l1.len()
    }
    pub fn l2_len(&self) -> usize {
        self.l2.len()
    }
    pub fn l3_len(&self) -> usize {
        self.l3.len()
    }

    pub fn is_l2_durable(&self, h: &[u8]) -> bool {
        self.l2.contains_key(h)
    }

    pub fn l3_present(&self, h: &[u8]) -> bool {
        self.l3.contains_key(h)
    }

    pub fn is_settled(&self, h: &[u8]) -> bool {
        self.l2.contains_key(h) || self.l3.contains_key(h)
    }

    pub fn has_durable_backing(&self, h: &[u8]) -> bool {
        self.is_settled(h)
    }

    pub fn pin_count(&self, h: &[u8]) -> u32 {
        self.pins.get(h).copied().unwrap_or(0)
    }

    /// ≈ SGLang `inc_lock_ref` — freeze demote while request/writeback holds the block.
    pub fn pin(&mut self, h: &[u8]) {
        *self.pins.entry(h.to_vec()).or_insert(0) += 1;
    }

    pub fn unpin(&mut self, h: &[u8]) -> Result<(), String> {
        let Some(c) = self.pins.get_mut(h) else {
            return Err("unpin: not pinned".into());
        };
        *c = c.saturating_sub(1);
        if *c == 0 {
            self.pins.remove(h);
        }
        Ok(())
    }

    /// Drop a block from all local tiers (PutEnd register/quota failure rollback).
    ///
    /// Does **not** reverse collateral L2→L3 demotes of *other* hashes from the
    /// same `put_durable` window — those stay. Call only for the session's own
    /// hashes after CP rejected registration (see PutEnd preflight).
    pub fn discard_settled(&mut self, h: &[u8]) {
        self.l0.remove(h);
        self.l1.remove(h);
        self.l2.remove(h);
        self.l3.remove(h);
        self.l0_order.retain(|x| x.as_slice() != h);
        self.l1_order.retain(|x| x.as_slice() != h);
        self.l2_order.retain(|x| x.as_slice() != h);
        self.pins.remove(h);
        self.one_shot.remove(h);
        self.promote_inflight.remove(h);
        self.hit_counts.remove(h);
        self.note_l2_absent(h);
    }

    pub(crate) fn l0_nbytes(&self, h: &[u8]) -> u64 {
        self.l0.get(h).map(|b| b.len() as u64).unwrap_or(0)
    }

    pub(crate) fn l2_nbytes(&self, h: &[u8]) -> u64 {
        self.l2.get(h).map(|b| b.len() as u64).unwrap_or(0)
    }

    /// Multi-hop promote cost estimate for `BandwidthPool` / `HitStats`.
    ///
    /// Hop 语义（与 [`promote_to_l0`] 实际 insert 次数对齐，供 P7 校准）：
    /// - 已在 L0 → `0`
    /// - 仅缺 L0（已在 L1）→ `1 * n`（L1→L0）
    /// - 在 L2、无 L1 → `2 * n`（L2→L1 + L1→L0）
    /// - 仅在 L3 → `3 * n`（L3→L2 fill + L2→L1 + L1→L0）
    ///
    /// 这是字节×跳数的骨架模型，不是实测带宽；真 workload 曲线见 `00-plan` P7 /
    /// #20（PR #31 review §4.5）。
    pub fn estimate_promote_cost(&self, h: &[u8]) -> u64 {
        let Some(n) = self.get(h).map(|b| b.len() as u64) else {
            return 0;
        };
        if self.l0.contains_key(h) {
            return 0;
        }
        let mut hops = 1u64; // into L0
        if !self.l1.contains_key(h) {
            hops += 1;
        }
        if !self.l2.contains_key(h) && self.l3.contains_key(h) {
            hops += 1; // L3→L2 fill
        }
        n * hops
    }

    fn touch(order: &mut VecDeque<Vec<u8>>, h: &[u8]) {
        order.retain(|x| x.as_slice() != h);
        order.push_back(h.to_vec());
    }

    /// PutEnd / 满块写回：只落 L2（F4 恢复点）。
    ///
    /// 对齐 `storage-layer.md`：写回不写 L1、不写 L3。L3 仅经 [`demote_l2_to_l3`]
    /// 或 L2 容量压力（迁移窗后稳态 XOR）。参考：SGLang 满块不写 host；
    /// Mooncake PutEnd 完成当前副本；Dynamo offload 链式 demote 而非 Put 双写。
    ///
    /// 写回时机由 agent 侧 `flush_every_n` 批量旋钮调节(见 `WritebackBatcher`):
    /// N=1 每满块即 flush(eager,F4 窗口最小);N 大则攒批 + 请求屏障兜底(lazy,
    /// ops ∝ 1/N,字节量不变,F4 窗口 ∝ N)。**备选暂不做(双轨注册)**:N 很大时
    /// "flush 后才注册"会让 radix 生长滞后 N 个块——备选方案是把注册拆两轨,
    /// 易失位置(块已在 L0,字节真在)即满即注册、durable 位置 flush 后补注册,
    /// 各说各的真话,不破坏 durable-first 不变量;N=2–4 时滞后仅 5–10s 且多轮
    /// 复用发生在请求屏障之后,损失可忽略,故暂缓。
    ///
    /// `ensure_l2_cap` 失败时**保证零变更**（两阶段：先扫描定受害者，确认可达
    /// cap 才应用；全 pinned 则 Err 且未动任何状态），因此 Err 路径只需撤销本次
    /// insert/touch，不存在"回滚本窗 demote"的需求。
    ///
    /// 覆盖写（同 hash 已有 L2）失败时**恢复原先 L2 字节**，不无条件 `remove`
    ///（幂等重放 / 重试安全）。新插入失败则撤掉本次 key。
    pub fn put_durable(
        &mut self,
        h: &[u8],
        bytes: &[u8],
    ) -> Result<(LocalTier, TierSideEffects), String> {
        let prior_l2 = self.l2.get(h).cloned();
        // S6(issue #74):轻量回滚记录——只记 h 在 l2_order 的原位置(O(N) 比较、
        // 无分配),替代原先全量 VecDeque clone(N 个 Vec<u8> 深拷贝,每次 put 都付)。
        // 配合 ensure_l2_cap 两阶段 Err 零变更,失败时弹出队尾 h 并按原位置插回
        // 即可逐位复原 LRU 顺序(PR #47「失败回滚不扰动 LRU」语义不变)。
        let h_prior_pos = self.l2_order.iter().position(|x| x.as_slice() == h);
        let had_placement = self.l2_arena.placement(h).is_some();
        self.l2.insert(h.to_vec(), bytes.to_vec());
        Self::touch(&mut self.l2_order, h);
        if !had_placement {
            self.note_l2_present(h);
        }
        let l2_demoted_to_l3 = match self.ensure_l2_cap() {
            Ok(d) => d,
            Err(e) => {
                // ensure_l2_cap Err = 零变更;只需撤销本次 insert/touch。
                match prior_l2 {
                    Some(old) => {
                        self.l2.insert(h.to_vec(), old);
                    }
                    None => {
                        self.l2.remove(h);
                        if !had_placement {
                            self.note_l2_absent(h);
                        }
                    }
                }
                // h 现居队尾(touch 放入、ensure_l2_cap 未动);弹出后按原位置
                // 插回(原本不在 = 新插入,截断即复原)。
                debug_assert_eq!(self.l2_order.back().map(|x| x.as_slice()), Some(h));
                self.l2_order.pop_back();
                if let Some(pos) = h_prior_pos {
                    self.l2_order.insert(pos, h.to_vec());
                }
                return Err(e);
            }
        };
        for victim in &l2_demoted_to_l3 {
            self.note_l2_absent(victim);
        }
        let effects = TierSideEffects {
            l0_demoted: Vec::new(),
            l2_demoted_to_l3,
        };
        if self.l2.contains_key(h) {
            Ok((LocalTier::L2, effects))
        } else if self.l3.contains_key(h) {
            // This hash itself was immediately demoted under L2 cap → L3 only.
            self.note_l2_absent(h);
            Ok((LocalTier::L3, effects))
        } else {
            Err("put_durable: block lost after L2 cap demote".into())
        }
    }

    /// Coldest-first LRU demote L2→L3。两阶段(S6, issue #74):先按索引扫描选定
    /// 受害者(不变更任何状态),确认可达 cap 才进入应用阶段;扫描发现全 pinned
    /// 则 Err 且**零变更**,调用方无需回滚 demote(替代旧 pop/rotate + 失败时
    /// 全量快照恢复)。受害者选择与旧实现一致:unpinned 中 coldest-first。
    fn ensure_l2_cap(&mut self) -> Result<Vec<Vec<u8>>, String> {
        let cap = self.caps.l2;
        if cap == 0 {
            return Ok(Vec::new());
        }
        // 阶段一:扫描(只读)。stale 条目(不在 l2)不计数,应用阶段顺带 GC——
        // 与旧实现 pop 后 continue 的清理语义一致。
        let mut demote_idx: Vec<usize> = Vec::new();
        let mut stale_idx: Vec<usize> = Vec::new();
        let mut evictable = self.l2.len();
        if evictable > cap {
            for (i, h) in self.l2_order.iter().enumerate() {
                if evictable <= cap {
                    break;
                }
                if !self.l2.contains_key(h) {
                    stale_idx.push(i);
                    continue;
                }
                // pinned 保持原位:旧实现 rotate 到队尾是 pop/push 的副产物,
                // 并非设计语义(pin 不应让块变热)。
                if self.pin_count(h) > 0 {
                    continue;
                }
                demote_idx.push(i);
                evictable -= 1;
            }
            if evictable > cap {
                return Err(format!(
                    "l2 over cap ({}>{cap}): all victims pinned — unpin before put",
                    self.l2.len()
                ));
            }
        }
        // 阶段二:应用(l2/l3 map 操作不可失败)。
        let mut demoted = Vec::with_capacity(demote_idx.len());
        for &i in &demote_idx {
            let victim = self.l2_order[i].clone();
            if let Some(bytes) = self.l2.remove(&victim) {
                // L3 已有 SSOT 时仅丢 L2 副本(同旧 `or_insert` 语义)。
                self.l3.entry(victim.clone()).or_insert(bytes);
                demoted.push(victim);
            }
        }
        if !demote_idx.is_empty() || !stale_idx.is_empty() {
            let drop: HashSet<usize> = demote_idx.iter().chain(&stale_idx).copied().collect();
            let mut i = 0usize;
            self.l2_order.retain(|_| {
                let keep = !drop.contains(&i);
                i += 1;
                keep
            });
        }
        Ok(demoted)
    }

    pub fn probe(&mut self, h: &[u8]) -> AccessKind {
        let kind = if self.l0.contains_key(h) {
            AccessKind::L0Hit
        } else if self.l1.contains_key(h) {
            AccessKind::L1Hit
        } else if self.l2.contains_key(h) {
            AccessKind::L2Hit
        } else if self.l3.contains_key(h) {
            AccessKind::L3Hit
        } else {
            AccessKind::Miss
        };
        if kind != AccessKind::Miss {
            *self.hit_counts.entry(h.to_vec()).or_insert(0) += 1;
            // one-shot 毕业后又达阈值 → 摘标记转热块待遇(不再驱逐最优先)。
            if kind == AccessKind::L0Hit
                && self.one_shot.contains(h)
                && self.hit_count(h) >= self.promote_admit_after
            {
                self.one_shot.remove(h);
            }
        }
        self.stats.record(kind);
        kind
    }

    pub fn get(&self, h: &[u8]) -> Option<&[u8]> {
        self.l0
            .get(h)
            .or_else(|| self.l1.get(h))
            .or_else(|| self.l2.get(h))
            .or_else(|| self.l3.get(h))
            .map(|v| v.as_slice())
    }

    pub fn local_tier(&self, h: &[u8]) -> Option<LocalTier> {
        if self.l0.contains_key(h) {
            Some(LocalTier::L0)
        } else if self.l1.contains_key(h) {
            Some(LocalTier::L1)
        } else if self.l2.contains_key(h) {
            Some(LocalTier::L2)
        } else if self.l3.contains_key(h) {
            Some(LocalTier::L3)
        } else {
            None
        }
    }

    /// Promote toward L0. Returns `(bytes_moved_estimate, collateral effects)`.
    pub fn promote_to_l0(&mut self, h: &[u8]) -> Result<(u64, TierSideEffects), String> {
        let mut effects = TierSideEffects::default();
        if self.l0.contains_key(h) {
            Self::touch(&mut self.l0_order, h);
            return Ok((0, effects));
        }
        let bytes = self
            .get(h)
            .ok_or_else(|| "promote: block missing all tiers".to_string())?
            .to_vec();
        let n = self.estimate_promote_cost(h).max(bytes.len() as u64);
        if !self.l2.contains_key(h) && self.l3.contains_key(h) {
            self.l2.insert(h.to_vec(), bytes.clone());
            Self::touch(&mut self.l2_order, h);
            self.note_l2_present(h);
            match self.ensure_l2_cap() {
                Ok(demoted) => {
                    for victim in &demoted {
                        self.note_l2_absent(victim);
                    }
                    effects.l2_demoted_to_l3 = demoted;
                }
                Err(e) => {
                    // ensure_l2_cap Err = 零变更;撤掉本次 insert/touch 即可。
                    self.l2.remove(h);
                    self.l2_order.retain(|x| x.as_slice() != h);
                    self.note_l2_absent(h);
                    return Err(e);
                }
            }
            if !self.is_settled(h) {
                return Err("promote: lost durable backing under L2 cap".into());
            }
        }
        if !self.l1.contains_key(h) {
            self.ensure_l1_room();
            self.l1.insert(h.to_vec(), bytes.clone());
            Self::touch(&mut self.l1_order, h);
        }
        effects.merge(self.ensure_l0_room()?);
        self.l0.insert(h.to_vec(), bytes);
        Self::touch(&mut self.l0_order, h);
        Ok((n, effects))
    }

    /// Demote L0. Requires durable backing; refuses if locally pinned (`lock_ref`).
    pub fn demote_l0(&mut self, h: &[u8]) -> Result<u64, String> {
        if self.pin_count(h) > 0 {
            return Err("demote_l0: pinned (lock_ref)".into());
        }
        if !self.has_durable_backing(h) {
            return Err("demote_l0: need L2 or L3 durable backing".into());
        }
        let n = self.l0.remove(h).map(|b| b.len() as u64).unwrap_or(0);
        self.l0_order.retain(|x| x.as_slice() != h);
        self.one_shot.remove(h);
        Ok(n)
    }

    pub fn fill_read_miss(&mut self, h: &[u8]) -> Result<(u64, TierSideEffects), String> {
        if self.l0.contains_key(h) {
            Self::touch(&mut self.l0_order, h);
            return Ok((0, TierSideEffects::default()));
        }
        self.promote_to_l0(h)
    }

    /// 带频率准入的 promote(决策 B:用完留不留)。
    ///
    /// hit_count ≥ `promote_admit_after` → 热块待遇(正常 LRU 位,未来预放置候选);
    /// < 阈值 → **one-shot**:照样进 L0(GPU 约束:attention 只读 HBM,不存在
    /// Mooncake 式"不搬直读"),但驱逐最优先、读完不挤兑热块——准入砍的是
    /// 一次性块引发的级联循环,不是 promote 本身。
    /// 对照:SGLang `write_through_selective`(hit_count≥2 才回写 L3)、
    /// Mooncake `FileStorage` CountMinSketch(DRAM↔SSD 提升频率门槛)。
    pub fn promote_to_l0_admitted(&mut self, h: &[u8]) -> Result<(u64, TierSideEffects), String> {
        let hot = self.hit_count(h) >= self.promote_admit_after;
        let r = self.promote_to_l0(h)?;
        if hot {
            self.one_shot.remove(h);
        } else {
            self.one_shot.insert(h.to_vec());
        }
        Ok(r)
    }

    /// 异步 promote 第一段:登记 in-flight(**去重**),不搬字节。
    ///
    /// 形态对齐 SGLang `prefetch_from_storage`(调度时发起、与排队/batching 重叠):
    /// agent 在 dispatch 收到预取清单即 `begin_promote`,`prepare_step` 只
    /// `finish_promote` 等残余。同块在途重复发起返回 [`BeginPromote::AlreadyInFlight`]
    /// (Dynamo offload 去重同款)。单进程原型两段式模拟;生产 begin 发起 RDMA
    /// 读、finish 收完成事件。
    pub fn begin_promote(&mut self, h: &[u8]) -> BeginPromote {
        if self.l0.contains_key(h) {
            return BeginPromote::AlreadyL0;
        }
        if self.get(h).is_none() {
            return BeginPromote::Missing;
        }
        if !self.promote_inflight.insert(h.to_vec()) {
            return BeginPromote::AlreadyInFlight;
        }
        BeginPromote::Started
    }

    /// 异步 promote 第二段:实际搬运(走准入判定)+ 清 in-flight 登记。
    /// 未经 begin 直接调用也可(等价同步 promote)。
    pub fn finish_promote(&mut self, h: &[u8]) -> Result<(u64, TierSideEffects), String> {
        self.promote_inflight.remove(h);
        self.promote_to_l0_admitted(h)
    }

    /// 在途 promote 数(探针/测试)。
    pub fn promote_inflight_len(&self) -> usize {
        self.promote_inflight.len()
    }

    pub fn evict_l0_pressure(&mut self) -> Result<Option<Vec<u8>>, String> {
        if self.caps.l0 == 0 || self.l0.len() < self.caps.l0 {
            return Ok(None);
        }
        // one-shot 优先:冷块 promote 后先被驱逐,不挤兑热块(准入砍级联的落点)。
        let victim = self
            .l0_order
            .iter()
            .find(|h| {
                self.one_shot.contains(h.as_slice())
                    && self.pin_count(h) == 0
                    && self.has_durable_backing(h.as_slice())
            })
            .or_else(|| {
                self.l0_order
                    .iter()
                    .find(|h| self.pin_count(h) == 0 && self.has_durable_backing(h.as_slice()))
            })
            .cloned()
            .ok_or_else(|| {
                "evict_l0: no durable unpinned victim (pin/writeback first)".to_string()
            })?;
        self.demote_l0(&victim)?;
        Ok(Some(victim))
    }

    fn ensure_l0_room(&mut self) -> Result<TierSideEffects, String> {
        let mut effects = TierSideEffects::default();
        let cap = self.caps.l0;
        if cap == 0 {
            return Ok(effects);
        }
        while self.l0.len() >= cap {
            match self.evict_l0_pressure()? {
                Some(v) => effects.l0_demoted.push(v),
                None => return Err("l0 full: cannot make room".into()),
            }
        }
        Ok(effects)
    }

    /// Drop coldest L1 until under cap.
    ///
    /// **静默、不进 [`TierSideEffects`]**：P4.3 从不把 L1 presence 发布到 CP
    ///（PutEnd/`register_request` 只报 L2；promote 只发 L0；L1=demotion-only）。
    /// 因此 L1 drop 无需 revoke。`LocationEvent::Present{L1}` / `apply_location_events`
    /// 的 L1 arm 为未来「满块顺便写 L1 / P7」预留，当前无生产者——若开始发 L1
    /// 事件，此处必须同步上报 Absent，否则视图漂移（#20；PR #31 review §4.2）。
    ///
    /// S8(issue #74 备忘,出处 issue #20 遗留表 4.2):L1 驱逐静默是**设计一致**
    ///（L1 presence 不进 CP 视图,视图无账可漂）;若将来 L1 presence 上视图,
    /// 本函数的驱逐必须同步 Absent 事件,否则 CP 镜像永久残留 L1 位置。
    fn ensure_l1_room(&mut self) {
        let cap = self.caps.l1;
        if cap == 0 {
            return;
        }
        while self.l1.len() >= cap {
            let Some(v) = self.l1_order.pop_front() else {
                break;
            };
            self.l1.remove(&v);
        }
    }

    /// Steady L2/L3 XOR: move L2 → L3, drop L2.
    pub fn demote_l2_to_l3(&mut self, h: &[u8]) -> Result<u64, String> {
        if self.pin_count(h) > 0 {
            return Err("demote_l2: pinned".into());
        }
        let bytes = self
            .l2
            .remove(h)
            .ok_or_else(|| "demote_l2: not on L2".to_string())?;
        let n = bytes.len() as u64;
        self.l2_order.retain(|x| x.as_slice() != h);
        self.l3.insert(h.to_vec(), bytes);
        self.note_l2_absent(h);
        Ok(n)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn durable_promote_demote() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 2,
            l1: 4,
            l2: 8,
        });
        e.put_durable(b"a", b"A").unwrap();
        assert!(!e.l3_present(b"a"), "PutEnd must not dual-write L3");
        e.promote_to_l0(b"a").unwrap();
        assert_eq!(e.local_tier(b"a"), Some(LocalTier::L0));
        e.demote_l0(b"a").unwrap();
        assert_eq!(e.local_tier(b"a"), Some(LocalTier::L1));
    }

    #[test]
    fn l0_pressure_reports_collateral() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 1,
            l1: 8,
            l2: 8,
        });
        e.put_durable(b"c0", b"0").unwrap();
        e.put_durable(b"c1", b"1").unwrap();
        e.promote_to_l0(b"c0").unwrap();
        let (_, fx) = e.promote_to_l0(b"c1").unwrap();
        assert_eq!(fx.l0_demoted, vec![b"c0".to_vec()]);
        assert_eq!(e.l0_len(), 1);
    }

    #[test]
    fn read_miss_from_l3() {
        let mut e = LocalTierEngine::new();
        e.l3.insert(b"z".to_vec(), b"ZZ".to_vec());
        e.fill_read_miss(b"z").unwrap();
        assert_eq!(e.local_tier(b"z"), Some(LocalTier::L0));
        assert!(e.is_l2_durable(b"z"));
    }

    #[test]
    fn l0_demote_allows_l3_only_backing() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 1,
            l1: 8,
            l2: 8,
        });
        e.put_durable(b"p", b"P").unwrap();
        e.promote_to_l0(b"p").unwrap();
        e.demote_l2_to_l3(b"p").unwrap();
        assert!(!e.is_l2_durable(b"p") && e.l3_present(b"p"));
        e.put_durable(b"q", b"Q").unwrap();
        let (_, fx) = e.promote_to_l0(b"q").unwrap();
        assert_eq!(fx.l0_demoted, vec![b"p".to_vec()]);
        assert_eq!(e.l0_len(), 1);
    }

    #[test]
    fn pin_blocks_demote_like_lock_ref() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 1,
            l1: 8,
            l2: 8,
        });
        e.put_durable(b"p", b"P").unwrap();
        e.promote_to_l0(b"p").unwrap();
        e.pin(b"p");
        e.put_durable(b"q", b"Q").unwrap();
        assert!(e.promote_to_l0(b"q").is_err());
        e.unpin(b"p").unwrap();
        let (_, fx) = e.promote_to_l0(b"q").unwrap();
        assert_eq!(fx.l0_demoted, vec![b"p".to_vec()]);
    }

    #[test]
    fn put_durable_l2_only_l3_via_cap_xor() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 1,
        });
        let (tx, d0) = e.put_durable(b"x", b"X").unwrap();
        assert_eq!(tx, LocalTier::L2);
        assert!(!e.l3_present(b"x"));
        assert!(d0.is_empty());
        // Cap pressure moves cold x L2→L3 (XOR), not PutEnd dual-write.
        let (ty, demoted) = e.put_durable(b"y", b"Y").unwrap();
        assert_eq!(ty, LocalTier::L2);
        assert_eq!(demoted.l2_demoted_to_l3, vec![b"x".to_vec()]);
        assert!(!e.is_l2_durable(b"x") && e.l3_present(b"x"));
        assert!(e.is_l2_durable(b"y") && !e.l3_present(b"y"));
    }

    #[test]
    fn pinned_cold_skipped_new_write_may_land_l3() {
        // x pinned → cap demote skips x; unpinned y can be demoted to L3 (still settled).
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 1,
        });
        e.put_durable(b"x", b"X").unwrap();
        e.pin(b"x");
        let (ty, fx) = e.put_durable(b"y", b"Y").unwrap();
        assert_eq!(ty, LocalTier::L3);
        assert_eq!(fx.l2_demoted_to_l3, vec![b"y".to_vec()]);
        assert!(e.is_l2_durable(b"x") && e.l3_present(b"y"));
    }

    /// PR #77 review:两阶段 `ensure_l2_cap` 的有意语义变更钉测——pinned 块在
    /// L2 压力下保持 LRU 原位,不再像旧实现 pop/rotate 到队尾(pin 是冻结语义,
    /// 不应让块变热)。若有人改回 rotate,本测试失败。
    #[test]
    fn s6_pinned_victim_keeps_l2_lru_position() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 3,
        });
        e.put_durable(b"x", b"X").unwrap();
        e.put_durable(b"a", b"A").unwrap();
        e.put_durable(b"b", b"B").unwrap();
        e.pin(b"x"); // 最冷块 pinned:扫描必经,旧实现会把它 rotate 到队尾
        e.put_durable(b"c", b"C").unwrap(); // 压力:demote 最冷 unpinned(a)
        assert!(e.l3_present(b"a") && !e.is_l2_durable(b"a"));
        let order: Vec<&[u8]> = e.l2_order.iter().map(Vec::as_slice).collect();
        assert_eq!(
            order,
            [b"x".as_slice(), b"b".as_slice(), b"c".as_slice()],
            "pinned x 必须保持队首原位(旧实现 rotate 后为 [b, x, c])"
        );
    }

    #[test]
    fn all_pinned_l2_over_cap_errors_and_rolls_back() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 1,
        });
        e.put_durable(b"x", b"X").unwrap();
        e.pin(b"x");
        // Force a second pinned L2 resident (bypass put_durable).
        e.l2.insert(b"z".to_vec(), b"Z".to_vec());
        e.l2_order.push_back(b"z".to_vec());
        e.pin(b"z");
        assert_eq!(e.l2_len(), 2);
        let err = e.put_durable(b"y", b"Y").unwrap_err();
        assert!(err.contains("pinned"), "{err}");
        assert!(!e.is_l2_durable(b"y"));
        assert!(
            !e.l3_present(b"y"),
            "Err must not leave y on L3 (cap demote then stuck-pinned)"
        );
        assert!(!e.is_settled(b"y"));
        assert_eq!(e.l2_len(), 2, "rollback y only");
        // Pre-existing pinned L2 residents untouched.
        assert!(e.is_l2_durable(b"x") && e.is_l2_durable(b"z"));
    }

    #[test]
    fn put_durable_err_restores_prior_l2_on_overwrite() {
        // Cap=2 with x,y pinned at capacity; force z pinned → over cap.
        // Overwrite y fails cap; must keep OLD bytes (not wipe the key).
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 2,
        });
        e.put_durable(b"y", b"OLD").unwrap();
        e.put_durable(b"x", b"X").unwrap();
        e.pin(b"x");
        e.pin(b"y");
        e.l2.insert(b"z".to_vec(), b"Z".to_vec());
        e.l2_order.push_back(b"z".to_vec());
        e.pin(b"z");
        assert_eq!(e.l2_len(), 3);
        let prior_order = e.l2_order.clone();
        let err = e.put_durable(b"y", b"NEW").unwrap_err();
        assert!(err.contains("pinned"), "{err}");
        assert_eq!(e.get(b"y"), Some(b"OLD".as_slice()));
        assert!(e.is_l2_durable(b"y"));
        assert!(!e.l3_present(b"y"));
        assert_eq!(e.l2_order, prior_order);
    }

    #[test]
    fn put_durable_err_also_undoes_collateral_l2_demotes() {
        // Cap=1; x pinned. Insert unpinned w on L2 (over cap). put y: demotes w to L3
        // then stuck (x pinned, y may demote…) — any Err must restore pre-put L2/L3.
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 1,
        });
        e.put_durable(b"x", b"X").unwrap();
        e.pin(b"x");
        e.l2.insert(b"w".to_vec(), b"W".to_vec());
        e.l2_order.push_front(b"w".to_vec()); // colder than x
        assert_eq!(e.l2_len(), 2);
        // Second pinned resident so after demoting w (+ maybe y) we still stick.
        e.l2.insert(b"z".to_vec(), b"Z".to_vec());
        e.l2_order.push_back(b"z".to_vec());
        e.pin(b"z");
        assert_eq!(e.l2_len(), 3);
        let prior_order = e.l2_order.clone();
        let err = e.put_durable(b"y", b"Y").unwrap_err();
        assert!(err.contains("pinned"), "{err}");
        assert!(!e.l3_present(b"y") && !e.is_settled(b"y"));
        // Collateral demote of w must be undone.
        assert!(e.is_l2_durable(b"w"), "w restored to L2");
        assert!(!e.l3_present(b"w"), "w not left on L3");
        assert_eq!(e.get(b"w"), Some(b"W".as_slice()));
        assert_eq!(e.l2_order, prior_order);
    }

    /// S6(issue #74):大 L2 队列下失败回滚逐位保序——轻量回滚记录(h 原位置)
    /// 与全量快照等效;新写与覆盖写两种失败路径都验证。
    #[test]
    fn put_durable_err_rollback_large_l2_order_preserved() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 1024,
        });
        // 1024 个 pinned 常驻块填满 cap。
        for i in 0..1024u32 {
            let h = format!("b{i:04}").into_bytes();
            e.put_durable(&h, b"V").unwrap();
            e.pin(&h);
        }
        // 强制再塞一个 pinned 常驻 → over cap。
        e.l2.insert(b"z".to_vec(), b"Z".to_vec());
        e.l2_order.push_back(b"z".to_vec());
        e.pin(b"z");
        let prior_order = e.l2_order.clone();
        // 新写失败(全 pinned):顺序逐位不变,块不留痕。
        let err = e.put_durable(b"y", b"Y").unwrap_err();
        assert!(err.contains("pinned"), "{err}");
        assert_eq!(e.l2_order, prior_order, "新写失败回滚后 LRU 逐位不变");
        assert!(!e.is_settled(b"y"));
        // 覆盖写失败(改写队首块):旧字节恢复,顺序逐位不变。
        let err = e.put_durable(b"b0000", b"NEW").unwrap_err();
        assert!(err.contains("pinned"), "{err}");
        assert_eq!(e.l2_order, prior_order, "覆盖写失败回滚后 LRU 逐位不变");
        assert_eq!(e.get(b"b0000"), Some(b"V".as_slice()));
    }

    #[test]
    fn hit_count_only_on_hits() {
        let mut e = LocalTierEngine::new();
        e.put_durable(b"a", b"A").unwrap();
        assert_eq!(e.probe(b"a"), AccessKind::L2Hit);
        assert_eq!(e.hit_count(b"a"), 1);
        e.probe(b"nobody");
        assert_eq!(e.hit_count(b"nobody"), 0, "miss 不计 hit_count");
    }

    #[test]
    fn admitted_promote_marks_cold_one_shot_and_graduates() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 4,
            l1: 8,
            l2: 8,
        });
        e.put_durable(b"cold", b"C").unwrap();
        e.put_durable(b"hot", b"H").unwrap();
        // cold: 首次命中(hit_count=1 < 2)→ one-shot。
        e.probe(b"cold");
        e.promote_to_l0_admitted(b"cold").unwrap();
        assert!(e.is_one_shot(b"cold"));
        // hot: 两次命中达阈值 → 热块待遇。
        e.probe(b"hot");
        e.probe(b"hot");
        e.promote_to_l0_admitted(b"hot").unwrap();
        assert!(!e.is_one_shot(b"hot"));
        // one-shot 毕业后(再命中达阈值)→ 摘标记。
        e.probe(b"cold"); // L0 hit,hit_count=2
        assert!(!e.is_one_shot(b"cold"), "达阈值后 L0 命中应毕业");
    }

    #[test]
    fn one_shot_evicted_before_hot() {
        let mut e = LocalTierEngine::with_caps(TierCaps {
            l0: 2,
            l1: 8,
            l2: 8,
        });
        e.put_durable(b"cold", b"C").unwrap();
        e.put_durable(b"hot", b"H").unwrap();
        e.probe(b"cold");
        e.promote_to_l0_admitted(b"cold").unwrap(); // one-shot
        e.probe(b"hot");
        e.probe(b"hot");
        e.promote_to_l0_admitted(b"hot").unwrap(); // hot
                                                   // 第三个块挤占 L0(cap=2):one-shot 的 cold 先被逐,即使 hot 更久没碰。
        e.put_durable(b"new", b"N").unwrap();
        e.promote_to_l0(b"new").unwrap();
        assert_eq!(e.local_tier(b"cold"), Some(LocalTier::L1), "one-shot 先逐");
        assert_eq!(e.local_tier(b"hot"), Some(LocalTier::L0), "热块留住");
    }

    #[test]
    fn async_promote_dedups_inflight() {
        let mut e = LocalTierEngine::new();
        e.put_durable(b"a", b"A").unwrap();
        assert_eq!(e.begin_promote(b"a"), BeginPromote::Started);
        assert_eq!(
            e.begin_promote(b"a"),
            BeginPromote::AlreadyInFlight,
            "同块在途不重复发起"
        );
        assert_eq!(e.promote_inflight_len(), 1);
        e.finish_promote(b"a").unwrap();
        assert_eq!(e.promote_inflight_len(), 0);
        assert_eq!(e.local_tier(b"a"), Some(LocalTier::L0));
        assert_eq!(e.begin_promote(b"a"), BeginPromote::AlreadyL0);
        assert_eq!(e.begin_promote(b"nobody"), BeginPromote::Missing);
    }
}
