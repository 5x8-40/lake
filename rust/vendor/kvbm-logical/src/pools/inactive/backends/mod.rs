// SPDX-FileCopyrightText: Copyright (c) 2024-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

//! Backend storage strategies for the inactive index.
//!
//! lake P4.2: pub only what controlplane needs (`MultiLru` + `Lineage`).
//! Other backends stay `pub(crate)` (BlockManager / avoid private_interfaces).

use super::*;

mod fifo;
mod hashmap_backend;
mod lineage;
mod lru_backend;
mod multi_lru_backend;
mod reuse_policy;

#[cfg(test)]
mod tests;

pub(crate) use fifo::FifoReusePolicy;
pub(crate) use hashmap_backend::HashMapBackend;
pub(crate) use lineage::LeafPolicy;
pub use lineage::LineageBackend;
pub(crate) use lru_backend::LruBackend;
pub use multi_lru_backend::MultiLruBackend;
pub(crate) use reuse_policy::ReusePolicy; // hashmap_backend: `use super::ReusePolicy`
