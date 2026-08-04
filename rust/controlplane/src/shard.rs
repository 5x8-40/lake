//! P4.9:一致性哈希分片环 + 扩缩迁移计划(逻辑)。
//!
//! 参考:Mooncake `MasterService` 1024 `MetadataShard` + `MakeTenantScopedKey` 分片;
//! lake 用带虚拟节点的一致性哈希环(扩容最小迁移 / 缩容 Drain)。
//! 关键差异:湖位置权威在 CP 内存;本切片只算所有权与迁移计划,**不做**跨机字节搬运(P5)。
//! Drain「推 L2」= 返回候选 id 列表(对齐 kv-cache-pool Drain 语义)。

use std::collections::{BTreeMap, HashMap, HashSet};

use lake_proto::lake::*;
use xxhash_rust::xxh3::xxh3_64;

use crate::authority::{Authority, NamespaceKey};

/// Default virtual nodes per physical KV Node.
pub const DEFAULT_VNODE_COUNT: u32 = 64;

fn block_identity_key(id: &KvBlockId) -> (String, String, i32, Vec<u8>) {
    (
        id.model_id.clone(),
        id.revision.clone(),
        id.pool_kind,
        id.block_hash.clone(),
    )
}

#[derive(Clone, Debug)]
struct NodeState {
    vnode_count: u32,
    draining: bool,
    vnode_hashes: Vec<u64>,
}

/// Consistent-hash ring: vnode hash → physical node_id.
#[derive(Clone, Debug, Default)]
pub struct ShardRing {
    generation: u64,
    default_vnode_count: u32,
    nodes: HashMap<String, NodeState>,
    /// Sorted ring: vnode_hash → node_id.
    ring: BTreeMap<u64, String>,
}

impl ShardRing {
    pub fn new(default_vnode_count: u32) -> Self {
        Self {
            generation: 0,
            default_vnode_count: default_vnode_count.max(1),
            nodes: HashMap::new(),
            ring: BTreeMap::new(),
        }
    }

    pub fn to_proto(&self) -> ShardMap {
        let mut nodes: Vec<ShardEntry> = self
            .nodes
            .iter()
            .map(|(id, st)| ShardEntry {
                node_id: id.clone(),
                vnode_count: st.vnode_count,
                draining: st.draining,
                vnode_hashes: st.vnode_hashes.clone(),
            })
            .collect();
        nodes.sort_by(|a, b| a.node_id.cmp(&b.node_id));
        ShardMap {
            generation: self.generation,
            default_vnode_count: self.default_vnode_count,
            nodes,
        }
    }

    fn rebuild_ring(&mut self) {
        self.ring.clear();
        for (node_id, st) in &self.nodes {
            if st.draining {
                // Draining nodes stay listed but leave the ownership ring
                // so keys remap to remaining live nodes.
                continue;
            }
            for &h in &st.vnode_hashes {
                self.ring.insert(h, node_id.clone());
            }
        }
    }

    fn vnode_hashes_for(node_id: &str, vnode_count: u32) -> Vec<u64> {
        let mut out = Vec::with_capacity(vnode_count as usize);
        for i in 0..vnode_count {
            let mut buf = Vec::with_capacity(node_id.len() + 8);
            buf.extend_from_slice(node_id.as_bytes());
            buf.extend_from_slice(&i.to_le_bytes());
            out.push(xxh3_64(&buf));
        }
        out.sort_unstable();
        out.dedup();
        // Rare hash collision among vnodes: pad with salt until count met.
        let mut salt = 0u64;
        while out.len() < vnode_count as usize {
            let mut buf = Vec::new();
            buf.extend_from_slice(node_id.as_bytes());
            buf.extend_from_slice(b"#");
            buf.extend_from_slice(&salt.to_le_bytes());
            let h = xxh3_64(&buf);
            if !out.contains(&h) {
                out.push(h);
            }
            salt += 1;
        }
        out.sort_unstable();
        out
    }

    /// Owner for a content hash. None if ring empty (or only draining).
    pub fn owner_of(&self, key: &[u8]) -> Option<String> {
        if self.ring.is_empty() {
            return None;
        }
        let h = xxh3_64(key);
        if let Some((_, node)) = self.ring.range(h..).next() {
            return Some(node.clone());
        }
        // wrap around
        self.ring.iter().next().map(|(_, n)| n.clone())
    }

    pub fn join(&mut self, node_id: &str, vnode_count: u32) -> Result<(), String> {
        if node_id.is_empty() {
            return Err("join: node_id required".into());
        }
        if self.nodes.contains_key(node_id) {
            return Err(format!("join: node {node_id} already present"));
        }
        let vc = if vnode_count == 0 {
            self.default_vnode_count
        } else {
            vnode_count
        };
        let vnode_hashes = Self::vnode_hashes_for(node_id, vc);
        self.nodes.insert(
            node_id.to_string(),
            NodeState {
                vnode_count: vc,
                draining: false,
                vnode_hashes,
            },
        );
        self.rebuild_ring();
        self.generation = self.generation.saturating_add(1);
        Ok(())
    }

    /// Mark draining and drop from ownership ring (keys remap).
    pub fn mark_drain(&mut self, node_id: &str) -> Result<(), String> {
        let st = self
            .nodes
            .get_mut(node_id)
            .ok_or_else(|| format!("drain: unknown node {node_id}"))?;
        if st.draining {
            return Err(format!("drain: node {node_id} already draining"));
        }
        st.draining = true;
        self.rebuild_ring();
        self.generation = self.generation.saturating_add(1);
        Ok(())
    }

    pub fn remove(&mut self, node_id: &str) -> Result<(), String> {
        let st = self
            .nodes
            .get(node_id)
            .ok_or_else(|| format!("remove: unknown node {node_id}"))?;
        if !st.draining {
            return Err(format!("remove: node {node_id} must DrainShardNode first"));
        }
        // Live ring must not still own keys for this node (already rebuilt without it).
        self.nodes.remove(node_id);
        self.rebuild_ring();
        self.generation = self.generation.saturating_add(1);
        Ok(())
    }
}

impl Authority {
    /// Snapshot shard map for `GetShardMap`.
    pub fn shard_map(&self) -> ShardMap {
        self.shard.to_proto()
    }

    pub fn shard_owner(&self, block_hash: &[u8]) -> Option<String> {
        self.shard.owner_of(block_hash)
    }

    /// Join node; return migrations of registered blocks whose owner became `node_id`.
    pub fn join_shard_node(
        &mut self,
        node_id: &str,
        vnode_count: u32,
    ) -> Result<(ShardMap, Vec<ShardMigration>), String> {
        // Capture old ownership for all known flats.
        let keys = self.all_block_keys();
        let mut before: HashMap<Vec<u8>, Option<String>> = HashMap::new();
        for (flat, _) in &keys {
            before.insert(flat.clone(), self.shard.owner_of(flat));
        }

        self.shard.join(node_id, vnode_count)?;

        let mut migrations = Vec::new();
        for (flat, id) in &keys {
            let old = before.get(flat).cloned().flatten();
            let new = self.shard.owner_of(flat);
            if new.as_deref() == Some(node_id) && old.as_deref() != Some(node_id) {
                if let Some(from) = old {
                    migrations.push(ShardMigration {
                        id: Some(id.clone()),
                        from_node: from,
                        to_node: node_id.to_string(),
                        push_l2_first: false,
                    });
                } else if let Some(to) = new {
                    // First nodes: no prior owner — not a migration.
                    let _ = to;
                }
            }
        }
        migrations.sort_by(|a, b| {
            a.id.as_ref()
                .map(|i| i.block_hash.clone())
                .cmp(&b.id.as_ref().map(|i| i.block_hash.clone()))
        });
        Ok((self.shard.to_proto(), migrations))
    }

    /// Drain node: remap ownership away; list migrations + L2 push candidates.
    pub fn drain_shard_node(
        &mut self,
        node_id: &str,
    ) -> Result<(ShardMap, Vec<ShardMigration>, Vec<KvBlockId>), String> {
        if !self.shard.nodes.contains_key(node_id) {
            return Err(format!("drain: unknown node {node_id}"));
        }
        let keys = self.all_block_keys();
        let mut before: HashMap<Vec<u8>, Option<String>> = HashMap::new();
        for (flat, _) in &keys {
            before.insert(flat.clone(), self.shard.owner_of(flat));
        }

        self.shard.mark_drain(node_id)?;

        let mut migrations = Vec::new();
        let mut push_l2: Vec<KvBlockId> = Vec::new();
        let mut push_seen: HashSet<(String, String, i32, Vec<u8>)> = HashSet::new();

        for (flat, id) in &keys {
            let old = before.get(flat).cloned().flatten();
            let new = self.shard.owner_of(flat);
            if old.as_deref() == Some(node_id) {
                // Owned by draining node → must leave; push L2 first (Drain 语义).
                if !push_seen.insert(block_identity_key(id)) {
                    continue;
                }
                push_l2.push(id.clone());
                if let Some(to) = new {
                    if to != node_id {
                        migrations.push(ShardMigration {
                            id: Some(id.clone()),
                            from_node: node_id.to_string(),
                            to_node: to,
                            push_l2_first: true,
                        });
                    }
                }
            }
        }

        // Also mark blocks that physically sit on this node's L2 even if hash
        // ownership already differed (placement vs ownership).
        for id in self.blocks_with_l2_on(node_id) {
            if push_seen.insert(block_identity_key(&id)) {
                push_l2.push(id);
            }
        }

        migrations.sort_by(|a, b| {
            a.id.as_ref()
                .map(|i| i.block_hash.clone())
                .cmp(&b.id.as_ref().map(|i| i.block_hash.clone()))
        });
        push_l2.sort_by(|a, b| a.block_hash.cmp(&b.block_hash));
        Ok((self.shard.to_proto(), migrations, push_l2))
    }

    /// Remove a drained node from the shard map.
    ///
    /// Ownership remap (Drain) ≠ physical migration done. Refuse while any
    /// L0/L1/L2 location still names this node — caller must clear placements
    /// (push L2 / migrate / Absent) first. Aligns Mooncake unmount with replica
    /// lifecycle; lake uses CP `locations` as the completion gate.
    pub fn remove_shard_node(&mut self, node_id: &str) -> Result<(), String> {
        let stuck = self.blocks_with_placement_on(node_id);
        if !stuck.is_empty() {
            return Err(format!(
                "remove: node {node_id} still has {} placement(s) in location view; complete migration/push_l2 first",
                stuck.len()
            ));
        }
        for (flat, _) in self.all_block_keys() {
            if self.shard.owner_of(&flat).as_deref() == Some(node_id) {
                return Err(format!(
                    "remove: node {node_id} still owns blocks; drain incomplete"
                ));
            }
        }
        self.shard.remove(node_id)
    }

    fn all_block_keys(&self) -> Vec<(Vec<u8>, KvBlockId)> {
        let mut out = Vec::new();
        let mut ns_keys: Vec<NamespaceKey> = self.namespaces.keys().cloned().collect();
        ns_keys.sort_by(|a, b| {
            a.model_id
                .cmp(&b.model_id)
                .then(a.revision.cmp(&b.revision))
        });
        for nk in ns_keys {
            let Some(ns) = self.namespaces.get(&nk) else {
                continue;
            };
            let mut pks: Vec<i32> = ns.pools.keys().copied().collect();
            pks.sort_unstable();
            for pk in pks {
                let Some(pool) = ns.pools.get(&pk) else {
                    continue;
                };
                for (flat, entry) in &pool.by_flat {
                    let id = entry.meta.id.clone().unwrap_or(KvBlockId {
                        model_id: nk.model_id.clone(),
                        revision: nk.revision.clone(),
                        pool_kind: pk,
                        block_hash: flat.clone(),
                        scope: "public".into(),
                    });
                    out.push((flat.clone(), id));
                }
            }
        }
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    }

    fn blocks_with_l2_on(&self, node_id: &str) -> Vec<KvBlockId> {
        self.blocks_with_placement_on_tier(node_id, Some(Tier::L2))
    }

    /// Blocks with any (or specific-tier) location on `node_id`.
    fn blocks_with_placement_on(&self, node_id: &str) -> Vec<KvBlockId> {
        self.blocks_with_placement_on_tier(node_id, None)
    }

    fn blocks_with_placement_on_tier(&self, node_id: &str, tier: Option<Tier>) -> Vec<KvBlockId> {
        let mut out = Vec::new();
        for nk in self.namespaces.keys() {
            let Some(ns) = self.namespaces.get(nk) else {
                continue;
            };
            for (&pk, pool) in &ns.pools {
                for (flat, entry) in &pool.by_flat {
                    let on = entry.meta.locations.iter().any(|l| {
                        l.node_id == node_id && tier.map(|t| l.tier == t as i32).unwrap_or(true)
                    });
                    if on {
                        out.push(entry.meta.id.clone().unwrap_or(KvBlockId {
                            model_id: nk.model_id.clone(),
                            revision: nk.revision.clone(),
                            pool_kind: pk,
                            block_hash: flat.clone(),
                            scope: "public".into(),
                        }));
                    }
                }
            }
        }
        out
    }
}

/// Test helper: fraction of keys that change owner when comparing two rings.
#[cfg(test)]
pub fn migration_ratio(before: &ShardRing, after: &ShardRing, keys: &[Vec<u8>]) -> f64 {
    if keys.is_empty() {
        return 0.0;
    }
    let mut moved = 0usize;
    for k in keys {
        if before.owner_of(k) != after.owner_of(k) {
            moved += 1;
        }
    }
    moved as f64 / keys.len() as f64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ring_membership_and_wrap() {
        let mut r = ShardRing::new(8);
        r.join("n0", 8).unwrap();
        r.join("n1", 8).unwrap();
        let map = r.to_proto();
        let mut ids: Vec<_> = map.nodes.iter().map(|n| n.node_id.clone()).collect();
        ids.sort();
        assert_eq!(ids, vec!["n0", "n1"]);
        // Every key gets an owner.
        for i in 0..100u64 {
            let k = i.to_le_bytes().to_vec();
            assert!(r.owner_of(&k).is_some());
        }
        assert!(map.generation >= 2);
    }

    #[test]
    fn expand_moves_only_new_interval() {
        let mut before = ShardRing::new(32);
        before.join("n0", 32).unwrap();
        before.join("n1", 32).unwrap();
        let keys: Vec<Vec<u8>> = (0..200u64).map(|i| format!("k{i}").into_bytes()).collect();
        let mut after = before.clone();
        after.join("n2", 32).unwrap();
        let ratio = migration_ratio(&before, &after, &keys);
        // With 3 nodes, ideal move ≈ 1/3; allow generous bound but far below 1.0.
        assert!(
            ratio < 0.55,
            "expand should be minimal migration, ratio={ratio}"
        );
        // All moved keys must land on n2.
        for k in &keys {
            let b = before.owner_of(k);
            let a = after.owner_of(k);
            if b != a {
                assert_eq!(a.as_deref(), Some("n2"));
            }
        }
    }

    // P6.5 判据(drain/迁移最小化验证):扩容→缩容全周期后,所有权完全恢复
    // 扩容前——扩缩只动该动的 key(最小迁移的往返闭环)。
    #[test]
    fn p65_join_drain_roundtrip_restores_ownership() {
        let mut ring = ShardRing::new(32);
        ring.join("n0", 32).unwrap();
        ring.join("n1", 32).unwrap();
        let keys: Vec<Vec<u8>> = (0..200u64).map(|i| format!("k{i}").into_bytes()).collect();
        let before = ring.clone();

        // 扩容:迁入新节点的比例有界(理想 ≈1/3)
        ring.join("n2", 32).unwrap();
        let expand_ratio = migration_ratio(&before, &ring, &keys);
        assert!(
            expand_ratio > 0.0 && expand_ratio < 0.55,
            "expand should move a bounded fraction, ratio={expand_ratio}"
        );

        // 缩容:drain(摘出 ownership)+ remove → 所有权逐 key 恢复扩容前
        ring.mark_drain("n2").unwrap();
        ring.remove("n2").unwrap();
        let restored = migration_ratio(&before, &ring, &keys);
        assert_eq!(
            restored, 0.0,
            "drain+remove round-trip should restore pre-join ownership exactly"
        );
    }
}
