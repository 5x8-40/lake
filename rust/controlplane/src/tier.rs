//! Presence marker types for `BlockRegistrationHandle::{mark_present,has_block}`.
//!
//! 参考:Dynamo presence `HashMap<TypeId,u32>`（`registry/handle.rs`）；
//! lake 用 unit struct 区分 L0/L1/L2（L3 用 `BlockMeta.l3_present`）。
//! `BlockMetadata` 在 kvbm-logical 上有 blanket impl（Clone+Send+Sync+'static）。

/// HBM / device (L0).
#[allow(dead_code)] // presence marker for future L0 publish path
#[derive(Clone, Copy, Debug, Default)]
pub struct TierL0;

/// Host DRAM (L1).
#[allow(dead_code)] // presence marker for future L1 publish path
#[derive(Clone, Copy, Debug, Default)]
pub struct TierL1;

/// NVMe / local durable (L2).
#[derive(Clone, Copy, Debug, Default)]
pub struct TierL2;
