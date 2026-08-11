//! P6.2: SubscribeView 的事件日志(replay buffer)与快照编码。
//!
//! 协议(`docs/architecture/control-plane.md`「粒度与协议」的单权威单流序号版):
//! - `seq = 0` 的 `ViewUpdate` = 全量快照;接收方**重置**镜像后应用。
//! - `seq >= 1` 单调递增;每次写调用提交一批事件(单写者持写锁提交,提交序即权威序)。
//! - `resume_from_seq = N`:重放 `(N, ...]` 的缓冲批;`N+1` 早于 buffer floor → 回退快照;
//!   `N` 达到/超过已发序号上界(CP 重启 next_seq 归 1、buffer 空)→ 同样回退快照(S2)。
//! - REGISTERED/MOVED 携带变更后**全量** locations + l3_present,接收方按 id upsert;
//!   INVALIDATED 只带 id,接收方删除。不推 ref_count(proto B1 注释);block_kind 仅
//!   REGISTERED 携带,接收方对 MOVED 忽略该字段。
//!
//! 参考:Dynamo `DeduplicatingStream` 的 `(publisher_id, sequence)` 去重——lake 单权威
//! 退化为单流 sequence,去重即 `seq <= last → skip`;replay 有界 + 超限回退快照与
//! Dynamo 的 replay 有界语义一致(`control-plane.md:122`)。

use std::collections::VecDeque;

use crate::{view_event::Kind, KvBlockId, Location, ViewEvent, ViewUpdate};

/// replay buffer 默认容量(ViewUpdate 批数)。超限逐出最旧批;过老的 resume 回退快照。
pub const DEFAULT_VIEW_LOG_CAP: usize = 1024;

/// 有界事件日志:gap replay 的权威来源。
pub(crate) struct ViewLog {
    updates: VecDeque<ViewUpdate>,
    cap: usize,
    next_seq: u64, // 1 起;0 保留给快照
}

impl Default for ViewLog {
    fn default() -> Self {
        Self {
            updates: VecDeque::new(),
            cap: DEFAULT_VIEW_LOG_CAP,
            next_seq: 1,
        }
    }
}

impl ViewLog {
    #[cfg(test)]
    pub(crate) fn with_cap(cap: usize) -> Self {
        Self {
            cap: cap.max(1),
            ..Self::default()
        }
    }

    /// 提交一批事件为一个带序号的 ViewUpdate(单写者,写锁内调用)。
    pub(crate) fn commit(&mut self, events: Vec<ViewEvent>) -> ViewUpdate {
        let u = ViewUpdate {
            seq: self.next_seq,
            events,
        };
        self.next_seq += 1;
        self.updates.push_back(u.clone());
        while self.updates.len() > self.cap {
            self.updates.pop_front();
        }
        u
    }

    /// 已提交的最后一个 seq(尚无事件时为 0)。
    pub(crate) fn last_seq(&self) -> u64 {
        self.next_seq - 1
    }

    /// buffer 中最旧 retained 批的 seq(空 buffer = next_seq,即无可重放)。
    fn floor(&self) -> u64 {
        self.updates.front().map(|u| u.seq).unwrap_or(self.next_seq)
    }

    /// 重放 `(from_seq, ...]` 的缓冲批;`from_seq + 1 < floor()` → None(回退快照)。
    ///
    /// S2(issue #74):`from_seq >= next_seq`(resume 点达到/超过本实例已发序号
    /// 上界,含重启后 buffer 空)→ None(回退快照)。单写者序号 1..next_seq 单调,
    /// 正确客户端的 resume 点必 ≤ last_seq = next_seq-1;超出上界说明客户端见过
    /// 本实例从未签发的 seq——CP 已重启(next_seq 归 1)或权威重建,其镜像状态
    /// 无法用重放证明有效,必须回退快照。「已最新」重连(from_seq == last_seq)
    /// 不命中该分支,仍走 `Some([])` 空重放,语义不变。
    ///
    /// 前提:`next_seq` 不跨重启持久化(进程重启归 1)。若将来经 CheckpointStore
    /// 恢复序号使 next_seq 跨重启连续,本分支语义须重估(届时 from_seq >= next_seq
    /// 不再蕴含「客户端见过未签发 seq」)。另:`restore_checkpoint` 的 reset() 保留
    /// next_seq 属 P6.2 已知边界,与本守卫正交。
    pub(crate) fn replay_after(&self, from_seq: u64) -> Option<Vec<ViewUpdate>> {
        // 先做零值/上界判断(无算术运算):`from_seq + 1` 在 from_seq = u64::MAX
        // 时 debug 构建溢出 panic、release 依赖 wrap(PR #77 review)。过本守卫后
        // from_seq ∈ [1, next_seq-1],后续 +1 不溢出。两个 None 分支是并集语义,
        // 换序不改变任何输入的返回。
        if from_seq == 0 || from_seq >= self.next_seq {
            return None;
        }
        if from_seq + 1 < self.floor() {
            return None;
        }
        Some(
            self.updates
                .iter()
                .filter(|u| u.seq > from_seq)
                .cloned()
                .collect(),
        )
    }

    /// 全清(restore_checkpoint 重建权威后调用;订阅者需重连拿快照——P6.2 已知边界)。
    pub(crate) fn reset(&mut self) {
        self.updates.clear();
    }
}

/// REGISTERED(首见)或 MOVED(位置/l3 变更):携带变更后全量 locations + l3_present。
pub(crate) fn upsert_event(
    kind: Kind,
    id: KvBlockId,
    locations: Vec<Location>,
    l3_present: bool,
    block_kind: i32,
) -> ViewEvent {
    ViewEvent {
        kind: kind as i32,
        id: Some(id),
        locations,
        l3_present,
        block_kind,
    }
}

/// INVALIDATED:只带 id。
pub(crate) fn invalidated_event(id: KvBlockId) -> ViewEvent {
    ViewEvent {
        kind: Kind::Invalidated as i32,
        id: Some(id),
        locations: Vec::new(),
        l3_present: false,
        block_kind: 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ev(flat: u8) -> ViewEvent {
        invalidated_event(KvBlockId {
            model_id: "m".into(),
            revision: String::new(),
            pool_kind: 0,
            block_hash: vec![flat],
            scope: "public".into(),
        })
    }

    #[test]
    fn view_log_seq_monotonic_and_bounded() {
        let mut log = ViewLog::with_cap(2);
        let u1 = log.commit(vec![ev(1)]);
        let u2 = log.commit(vec![ev(2)]);
        let u3 = log.commit(vec![ev(3)]);
        assert_eq!((u1.seq, u2.seq, u3.seq), (1, 2, 3));
        assert_eq!(log.last_seq(), 3);
        assert_eq!(log.floor(), 2); // cap=2 逐出 seq=1
    }

    #[test]
    fn view_log_replay_window() {
        let mut log = ViewLog::with_cap(2);
        for i in 1..=4 {
            log.commit(vec![ev(i as u8)]);
        }
        // buffer 持有 [3,4];resume from 2 → 重放 3,4
        let replayed = log.replay_after(2).expect("in window");
        assert_eq!(
            replayed.iter().map(|u| u.seq).collect::<Vec<_>>(),
            vec![3, 4]
        );
        // resume from 1 → 1+1=2 < floor=3 → 回退快照
        assert!(log.replay_after(1).is_none());
        // resume from 0 由 handler 走快照,日志侧同样拒绝
        assert!(log.replay_after(0).is_none());
        // 已最新 → 空重放(非回退)
        assert_eq!(log.replay_after(4).expect("current").len(), 0);
    }

    /// S2(issue #74):resume 点达到/超过已发序号上界 → None(回退快照)。
    /// CP 重启后 next_seq 归 1、buffer 空,旧 resume_from_seq(如 100)必须
    /// 触发快照,而不是 `Some([])` 空重放(那会让镜像带着旧状态静默发散)。
    #[test]
    fn view_log_replay_resume_ahead_of_issued_seq() {
        // 重启态:空 buffer、next_seq=1;任何非零 resume 都超上界。
        let log = ViewLog::default();
        assert!(log.replay_after(100).is_none());
        assert!(log.replay_after(1).is_none());

        // 有事件:窗口内正常重放;已最新仍空重放;超上界 → None。
        let mut log = ViewLog::default();
        for i in 1..=3u8 {
            log.commit(vec![ev(i)]);
        }
        assert_eq!(log.replay_after(1).expect("in window").len(), 2);
        assert_eq!(log.replay_after(3).expect("current").len(), 0);
        assert!(log.replay_after(4).is_none(), "from_seq == next_seq → 快照");
        assert!(
            log.replay_after(100).is_none(),
            "from_seq >> next_seq → 快照"
        );
    }

    /// PR #77 review:恶意/损坏客户端传 u64::MAX 时,`from_seq + 1` 不得溢出
    /// (debug 构建 panic);上界守卫先命中 → None(回退快照)。
    #[test]
    fn view_log_replay_max_seq_no_overflow() {
        let mut log = ViewLog::default();
        log.commit(vec![ev(1)]);
        assert!(log.replay_after(u64::MAX).is_none());
        assert!(log.replay_after(u64::MAX - 1).is_none());
    }
}
