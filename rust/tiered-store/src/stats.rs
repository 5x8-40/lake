//! 分层命中 / 成本曲线计数（P4.3 验证）。

/// Relative cost units for a hit at each tier (draft; P7 calibrate).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TierCost {
    L0 = 1,
    L1 = 4,
    L2 = 20,
    L3 = 200,
    Miss = 1000,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AccessKind {
    L0Hit,
    L1Hit,
    L2Hit,
    L3Hit,
    Miss,
}

impl AccessKind {
    pub fn cost(self) -> u64 {
        match self {
            Self::L0Hit => TierCost::L0 as u64,
            Self::L1Hit => TierCost::L1 as u64,
            Self::L2Hit => TierCost::L2 as u64,
            Self::L3Hit => TierCost::L3 as u64,
            Self::Miss => TierCost::Miss as u64,
        }
    }
}

#[derive(Debug, Default, Clone)]
pub struct HitStats {
    pub l0: u64,
    pub l1: u64,
    pub l2: u64,
    pub l3: u64,
    pub miss: u64,
    pub total_cost: u64,
}

impl HitStats {
    pub fn record(&mut self, kind: AccessKind) {
        match kind {
            AccessKind::L0Hit => self.l0 += 1,
            AccessKind::L1Hit => self.l1 += 1,
            AccessKind::L2Hit => self.l2 += 1,
            AccessKind::L3Hit => self.l3 += 1,
            AccessKind::Miss => self.miss += 1,
        }
        self.total_cost += kind.cost();
    }

    pub fn accesses(&self) -> u64 {
        self.l0 + self.l1 + self.l2 + self.l3 + self.miss
    }

    /// Hit rate excluding cold miss (L0–L3 hits / all accesses).
    pub fn hit_rate(&self) -> f64 {
        let n = self.accesses();
        if n == 0 {
            return 0.0;
        }
        (self.l0 + self.l1 + self.l2 + self.l3) as f64 / n as f64
    }

    pub fn avg_cost(&self) -> f64 {
        let n = self.accesses();
        if n == 0 {
            return 0.0;
        }
        self.total_cost as f64 / n as f64
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::{LocalTierEngine, TierCaps};

    #[test]
    fn hit_rate_and_cost_curve() {
        let mut e = LocalTierEngine::with_caps(TierCaps::default());
        e.put_durable(b"hot", b"H").unwrap();
        let _ = e.promote_to_l0(b"hot").unwrap();
        e.put_durable(b"warm", b"W").unwrap();
        e.put_durable(b"cold", b"C").unwrap();
        e.demote_l2_to_l3(b"cold").unwrap(); // L3 only via cold demote

        assert_eq!(e.probe(b"hot").cost(), TierCost::L0 as u64);
        assert_eq!(e.probe(b"warm").cost(), TierCost::L2 as u64);
        assert_eq!(e.probe(b"cold").cost(), TierCost::L3 as u64);
        assert_eq!(e.probe(b"miss").cost(), TierCost::Miss as u64);

        assert!((e.stats.hit_rate() - 0.75).abs() < 1e-9);
        assert!(e.stats.avg_cost() > TierCost::L0 as u64 as f64);
        assert!(e.stats.avg_cost() < TierCost::Miss as u64 as f64);
    }
}
