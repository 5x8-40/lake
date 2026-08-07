//! 写回批量旋钮 `flush_every_n`(per-agent 配置;P7 收口决策 1)。
//!
//! 语义:decode 流式产出的满块先入缓冲,攒满 N 块才 flush 落 L2;请求结束屏障
//! 兜底 flush 全部残余(F4 窗口归零);后台带宽空闲可提前 flush(错峰,归
//! [`BandwidthPool`] 余量驱动,原型不模拟)。
//!
//! 三维权衡(P7.3 已校准字节量与 N 无关,见 `kv-cache-pool.md`「P7.3 校准结论」):
//! - ops/RPC 次数 ∝ 1/N;
//! - F4 丢失窗口 ∝ N(未 flush 段随生产节点故障丢失;SLO 记账按集群最大 N);
//! - radix 生长时效 ∝ N(注册在 flush 后,durable-first;滞后备选=双轨注册,
//!   见 `engine.rs::put_durable` 注释,暂不做)。
//!
//! per-agent 无需跨节点一致:durable-first 不变量按块成立,与 flush 时机无关;
//! 不同节点配不同 N 只影响各自产出块的 F4 窗口。
//!
//! 参考:SGLang HiCache `write_backup` 批量回写(host 批量 + 线程);差异是
//! SGLang 按容量压力(evict 时)批量,我们按块数预算批量(eager/lazy 连续谱)。

use std::collections::VecDeque;

/// 满块写回缓冲:攒 N 块出一批。
pub struct WritebackBatcher {
    /// 每批块数(1 = eager 每块即 flush;越大越 lazy)。per-agent 配置。
    flush_every_n: usize,
    pending: VecDeque<(Vec<u8>, Vec<u8>)>,
}

impl WritebackBatcher {
    pub fn new(flush_every_n: usize) -> Self {
        Self {
            flush_every_n: flush_every_n.max(1),
            pending: VecDeque::new(),
        }
    }

    pub fn flush_every_n(&self) -> usize {
        self.flush_every_n
    }

    pub fn pending_len(&self) -> usize {
        self.pending.len()
    }

    /// 满块入缓冲;攒满 N 返回一批待 flush,否则 None。
    pub fn push(&mut self, hash: Vec<u8>, bytes: Vec<u8>) -> Option<Vec<(Vec<u8>, Vec<u8>)>> {
        self.pending.push_back((hash, bytes));
        if self.pending.len() >= self.flush_every_n {
            Some(self.drain())
        } else {
            None
        }
    }

    /// 屏障/闲时兜底:取出全部残余(请求结束必须调用,F4 窗口归零)。
    pub fn drain(&mut self) -> Vec<(Vec<u8>, Vec<u8>)> {
        self.pending.drain(..).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn batches_every_n_and_drains_rest() {
        let mut b = WritebackBatcher::new(3);
        assert!(b.push(b"a".to_vec(), b"A".to_vec()).is_none());
        assert!(b.push(b"b".to_vec(), b"B".to_vec()).is_none());
        let batch = b
            .push(b"c".to_vec(), b"C".to_vec())
            .expect("3rd triggers flush");
        assert_eq!(batch.len(), 3);
        assert_eq!(b.pending_len(), 0);

        assert!(b.push(b"d".to_vec(), b"D".to_vec()).is_none());
        let rest = b.drain();
        assert_eq!(rest.len(), 1);
        assert_eq!(b.pending_len(), 0);
    }

    #[test]
    fn n_one_is_eager() {
        let mut b = WritebackBatcher::new(1);
        assert_eq!(b.push(b"a".to_vec(), b"A".to_vec()).unwrap().len(), 1);
    }
}
