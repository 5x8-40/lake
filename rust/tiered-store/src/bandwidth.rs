//! 后台迁移带宽池骨架（`<10%` 节流，可暂停）。
//!
//! P4.3：令牌桶计数，供 promote/demote/GC 共享；真网络节流 P5。

/// Shared background bandwidth budget (bytes per window).
#[derive(Debug, Clone)]
pub struct BandwidthPool {
    /// Max bytes movable in one `tick` (soft cap).
    pub window_bytes: u64,
    /// Remaining budget in the current window.
    remaining: u64,
    /// When true, all migrations are no-ops.
    pub paused: bool,
}

impl BandwidthPool {
    pub fn new(window_bytes: u64) -> Self {
        Self {
            window_bytes,
            remaining: window_bytes,
            paused: false,
        }
    }

    /// Default: 10% of a notional 1 GiB/s link over a 1s window → ~100 MiB.
    pub fn default_throttle() -> Self {
        Self::new(100 * 1024 * 1024)
    }

    pub fn pause(&mut self) {
        self.paused = true;
    }

    pub fn resume(&mut self) {
        self.paused = false;
    }

    pub fn reset_window(&mut self) {
        self.remaining = self.window_bytes;
    }

    pub fn remaining(&self) -> u64 {
        self.remaining
    }

    /// Try to consume `bytes`. Returns false if paused or over budget.
    pub fn try_consume(&mut self, bytes: u64) -> bool {
        if self.paused {
            return false;
        }
        if bytes > self.remaining {
            return false;
        }
        self.remaining -= bytes;
        true
    }

    /// Return unused budget (e.g. action failed after reserve).
    pub fn refund(&mut self, bytes: u64) {
        self.remaining = (self.remaining + bytes).min(self.window_bytes);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn throttle_and_pause() {
        let mut p = BandwidthPool::new(100);
        assert!(p.try_consume(40));
        assert!(!p.try_consume(70));
        p.pause();
        assert!(!p.try_consume(1));
        p.resume();
        p.reset_window();
        assert!(p.try_consume(100));
    }
}
