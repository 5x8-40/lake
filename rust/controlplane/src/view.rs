//! P6.2: SubscribeView 的事件日志(replay buffer)与快照编码。
//!
//! 协议(`docs/architecture/control-plane.md`「粒度与协议」的单权威单流序号版):
//! - `seq = 0` 的 `ViewUpdate` = 全量快照;接收方**重置**镜像后应用。
//! - `seq >= 1` 单调递增;每次写调用提交一批事件(单写者持写锁提交,提交序即权威序)。
//! - `resume_from_seq = N`:重放 `(N, ...]` 的缓冲批;`N+1` 早于 buffer floor → 回退快照。
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
    pub(crate) fn replay_after(&self, from_seq: u64) -> Option<Vec<ViewUpdate>> {
        if from_seq == 0 || from_seq + 1 < self.floor() {
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
}
