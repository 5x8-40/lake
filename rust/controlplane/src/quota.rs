//! P4.6 per-namespace soft/hard quota + borrow accounting.
//!
//! Reference: Mooncake `MasterService::ReserveTenantQuota` /
//! `ComputeTenantQuotaDeficit` (tenant hard ceiling + reserved/used);
//! LMCache `QuotaManager::set_quota` / `get_limit_bytes` (CRUD registry).
//!
//! Critical diffs vs references:
//! - Mooncake = tenant-level single ceiling; lake = per `(model_id,revision)`
//!   soft + hard + optional borrow from pool free.
//! - LMCache = single limit driving eviction cycles; lake returns
//!   `BackpressureSignal` on hard hit (gateway sheds; pool does not).
//! - `soft_bytes==0` / `hard_bytes==0` = that side unlimited (P4.5 compat).

use lake_proto::lake::{BackpressureSignal, Quota};

/// Outcome of a charged write admission check.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AdmitWrite {
    /// Under soft, or soft unlimited.
    WithinSoft,
    /// Over soft, under hard; may use borrow / own soft eviction.
    OverSoft,
    /// Would exceed hard after best-effort eviction — reject + signal.
    HardQuota {
        used_bytes: i64,
        soft_bytes: i64,
        hard_bytes: i64,
        deficit_bytes: i64,
    },
    /// Under per-namespace hard quota, but the shared pool has no free or
    /// reclaimable borrowed capacity for this over-soft write.
    PoolCapacity {
        used_bytes: i64,
        soft_bytes: i64,
        hard_bytes: i64,
        deficit_bytes: i64,
    },
}

impl AdmitWrite {
    pub fn backpressure(&self, model_id: &str, revision: &str) -> Option<BackpressureSignal> {
        match self {
            AdmitWrite::HardQuota {
                used_bytes,
                soft_bytes,
                hard_bytes,
                deficit_bytes,
            } => Some(BackpressureSignal {
                model_id: model_id.into(),
                revision: revision.into(),
                used_bytes: *used_bytes,
                soft_bytes: *soft_bytes,
                hard_bytes: *hard_bytes,
                deficit_bytes: *deficit_bytes,
                reason: "HARD_QUOTA".into(),
            }),
            AdmitWrite::PoolCapacity {
                used_bytes,
                soft_bytes,
                hard_bytes,
                deficit_bytes,
            } => Some(BackpressureSignal {
                model_id: model_id.into(),
                revision: revision.into(),
                used_bytes: *used_bytes,
                soft_bytes: *soft_bytes,
                hard_bytes: *hard_bytes,
                deficit_bytes: *deficit_bytes,
                reason: "POOL_CAPACITY".into(),
            }),
            _ => None,
        }
    }
}

pub fn quota_or_default(q: Option<&Quota>) -> Quota {
    q.cloned().unwrap_or(Quota {
        soft_bytes: 0,
        hard_bytes: 0,
        borrow_enabled: false,
    })
}

/// Shared validator for `RegisterModel` / `SetModelQuota`.
///
/// `soft/hard == 0` means that side is unlimited (P4.5 compat). Negative or
/// `soft > hard` (when hard > 0) is rejected so callers cannot bypass thresholds.
pub fn validate_quota(q: &Quota) -> Result<(), String> {
    if q.soft_bytes < 0 || q.hard_bytes < 0 {
        return Err("quota: soft/hard_bytes must be non-negative".into());
    }
    if q.hard_bytes > 0 && q.soft_bytes > q.hard_bytes {
        return Err("quota: soft_bytes must be <= hard_bytes when hard>0".into());
    }
    Ok(())
}

/// Bytes borrowed beyond soft (0 if soft unlimited or under soft).
pub fn borrowed_bytes(used: i64, soft: i64) -> i64 {
    if soft <= 0 {
        return 0;
    }
    (used - soft).max(0)
}

/// Classify write of `delta` bytes given current `used` and quota.
/// Caller must already apply best-effort own soft eviction / borrow reclaim
/// if desired; this is the pure threshold check on projected usage.
pub fn classify_write(used: i64, delta: i64, quota: &Quota) -> AdmitWrite {
    let projected = used.saturating_add(delta);
    let soft = quota.soft_bytes;
    let hard = quota.hard_bytes;

    if hard > 0 && projected > hard {
        return AdmitWrite::HardQuota {
            used_bytes: used,
            soft_bytes: soft,
            hard_bytes: hard,
            deficit_bytes: projected - hard,
        };
    }
    if soft > 0 && projected > soft {
        return AdmitWrite::OverSoft;
    }
    AdmitWrite::WithinSoft
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unlimited_when_zero() {
        let q = Quota {
            soft_bytes: 0,
            hard_bytes: 0,
            borrow_enabled: false,
        };
        assert_eq!(classify_write(1_000_000, 1, &q), AdmitWrite::WithinSoft);
    }

    #[test]
    fn hard_rejects() {
        let q = Quota {
            soft_bytes: 100,
            hard_bytes: 200,
            borrow_enabled: true,
        };
        match classify_write(180, 50, &q) {
            AdmitWrite::HardQuota { deficit_bytes, .. } => assert_eq!(deficit_bytes, 30),
            other => panic!("expected HardQuota, got {other:?}"),
        }
    }

    #[test]
    fn soft_over_under_hard() {
        let q = Quota {
            soft_bytes: 100,
            hard_bytes: 200,
            borrow_enabled: true,
        };
        assert_eq!(classify_write(90, 20, &q), AdmitWrite::OverSoft);
        assert_eq!(borrowed_bytes(120, 100), 20);
    }

    #[test]
    fn validate_rejects_negative_and_soft_gt_hard() {
        assert!(validate_quota(&Quota {
            soft_bytes: -1,
            hard_bytes: 0,
            borrow_enabled: false,
        })
        .is_err());
        assert!(validate_quota(&Quota {
            soft_bytes: 200,
            hard_bytes: 100,
            borrow_enabled: false,
        })
        .is_err());
        assert!(validate_quota(&Quota {
            soft_bytes: 0,
            hard_bytes: 0,
            borrow_enabled: true,
        })
        .is_ok());
    }
}
