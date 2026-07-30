//! `Location.tier` 硬约束校验。
//!
//! `schema.proto`:Tier 枚举含 L3 仅为四层模型完整;Location 只允许 L0/L1/L2。

use lake_proto::lake::{Location, Tier};

use crate::error::{Result, TransferError};

/// 拒绝 `tier=L3` / UNSPECIFIED / 未知值。
pub fn validate_location_tier(loc: &Location) -> Result<()> {
    match Tier::try_from(loc.tier) {
        Ok(Tier::L0) | Ok(Tier::L1) | Ok(Tier::L2) => Ok(()),
        Ok(other) => Err(TransferError::InvalidTier(other as i32)),
        Err(_) => Err(TransferError::InvalidTier(loc.tier)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn loc(tier: Tier) -> Location {
        Location {
            tier: tier as i32,
            node_id: "n0".into(),
            segment_id: 1,
            offset: 0,
        }
    }

    #[test]
    fn accepts_l0_l1_l2() {
        validate_location_tier(&loc(Tier::L0)).unwrap();
        validate_location_tier(&loc(Tier::L1)).unwrap();
        validate_location_tier(&loc(Tier::L2)).unwrap();
    }

    #[test]
    fn rejects_l3() {
        let err = validate_location_tier(&loc(Tier::L3)).unwrap_err();
        assert!(matches!(err, TransferError::InvalidTier(_)));
    }

    #[test]
    fn rejects_unspecified() {
        assert!(validate_location_tier(&loc(Tier::Unspecified)).is_err());
    }
}
