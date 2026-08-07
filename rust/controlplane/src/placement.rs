//! P7.6(B2):池侧稳态预放置——跟随流量 + 共享确定性哈希(HRW)预放置。
//!
//! 扩容 join 的一次性 warmup(`shard.rs::warmup_plan`)之外,本模块补上稳态
//! 放置循环的两条路径(方案 Z:放置归池,Router 只读视图不指挥):
//!
//! - **(b) 跟随流量**:`ReportHits` 的命中按 per-(block,node) 计数,过阈值
//!   且块不在该节点 L0 时,对该节点触发放置(带滞回与前缀祖先共置)。
//! - **(a) HRW 预放置**:`RegisterBlocks` 时按 HRW(链锚点, ready 节点集)
//!   把链预放到「家节点」——无权纯函数,与 Go Router 冷路径的负载权重
//!   HRW 在负载均衡(权重相等)时 argmax 一致,两侧朝同一节点收敛。
//!
//! 参考:
//! - Dynamo `lib/kv-router/src/protocols.rs::WorkerSelectionResult`
//!   (`overlap_blocks`/`effective_overlap_blocks` 打分选点):B1 亲和选路的
//!   打分原型;本模块 (a) 是其对偶——选点归 Router,池按同一锚点哈希把链
//!   预放到均衡时 Router 会选的节点。
//! - SGLang `sgl-model-gateway/src/policies/cache_aware.rs`(近似树 + 失衡
//!   退最短队列):命中感知路由的工业形态;lake 差异——放置闭环在池(权威
//!   位置视图 + per-(block,node) 热度),非网关侧按文本历史猜。
//! - SGLang `radix_cache.py::TreeNode.hit_count`:按节点累计命中;lake 扩成
//!   per-(block,node) 以识别「热在哪个节点」并就地放置。
//!
//! 关键差异:reference 的预取/溢出是引擎私有缓存行为;lake L0 归存储池统一
//! 放置(方案 Z),本模块是池权威发起的稳态放置,引擎/调度器均不指挥。
//! 节流说明:此处用**计数节流**(阈值 + 单次评估限量);真字节级节流(后台
//! 带宽池 <10%)归 P5 `BandwidthPool`。

use std::collections::HashSet;

use lake_proto::lake::*;

use crate::authority::{resolve_pool_kind, Authority, NamespaceKey};

/// 跟随流量触发放置的命中阈值:同一 (block,node) 计数达到即评估放置。
pub const FOLLOW_HIT_THRESHOLD: u32 = 2;
/// 单次 ReportHits 评估的候选上限(top 16,计数节流)。
pub const FOLLOW_EVAL_LIMIT: usize = 16;
/// 前缀锚点深度上限(与 Go Router 冷路径路由键同源):深度 1 会被全局系统
/// 前缀打成热点,取 `hashes[min(len,8)-1]`。
pub const ANCHOR_DEPTH_CAP: usize = 8;

/// FNV-1a 64(Go `hash/fnv` New64a 同算法,跨语言逐位一致)。不引第三方 crate。
const FNV_OFFSET: u64 = 14695981039346656037;
const FNV_PRIME: u64 = 1099511628211;

fn fnv1a64_update(mut h: u64, bytes: &[u8]) -> u64 {
    for &b in bytes {
        h ^= u64::from(b);
        h = h.wrapping_mul(FNV_PRIME);
    }
    h
}

/// HRW(rendezvous)打分:`fnv1a64(key_len(u64 LE) ‖ key ‖ node_id)`。
/// 字节布局与 Go Router `pickNodeForRequest` 冷路径严格一致——Go 侧同为
/// 纯整数 argmax + 节点 id 决胜(负载经护栏过滤表达,不进 score),故
/// 家节点未过载(过护栏)时,池侧预放置目标与 Router 冷路径选点**逐点
/// 相同**;家节点过载时 Go 溢到次高分节点(有意分叉,放置为建议性,
/// follow_traffic 随流量修正)。
pub fn hrw_score(key: &[u8], node: &str) -> u64 {
    let mut h = FNV_OFFSET;
    h = fnv1a64_update(h, &(key.len() as u64).to_le_bytes());
    h = fnv1a64_update(h, key);
    fnv1a64_update(h, node.as_bytes())
}

/// 有界深度前缀锚点:`hashes[min(len, ANCHOR_DEPTH_CAP)-1]`。
pub fn chain_anchor(prefix_hashes: &[Vec<u8>]) -> Option<&[u8]> {
    if prefix_hashes.is_empty() {
        return None;
    }
    let depth = prefix_hashes.len().min(ANCHOR_DEPTH_CAP);
    Some(&prefix_hashes[depth - 1])
}

/// HRW 家节点选择:score 大者胜,平局取节点 id **较小**者——与 Go 冷路径
/// `score > bestScore || (score == bestScore && id < best)` 严格同向。
/// score 注入以便单测构造平局(真实 fnv u64 平局不可构造)。
fn pick_home<'a, I>(nodes: I, score_of: impl Fn(&str) -> u64) -> Option<String>
where
    I: IntoIterator<Item = &'a String>,
{
    nodes
        .into_iter()
        .max_by(|a, b| score_of(a).cmp(&score_of(b)).then_with(|| b.cmp(a)))
        .cloned()
}

/// (block, node) 放置滞回标记:已下发过放置计划的对,不重复下发
/// (真放置经 agent `PlaceBlocks` 异步完成;标记防计划风暴)。
pub(crate) type PlacementMarks = HashSet<(Vec<u8>, String)>;

impl Authority {
    /// (a) register 后的共享确定性预放置。
    ///
    /// HRW(链锚点, ready 节点集) 选出链的家节点,把链中已注册且不在家节点
    /// L0 的块按根→叶序规划放置(经 `WarmupSink` 下发,字节搬运归 P5)。
    /// 稳态常见情形(生产节点==家节点,新块已在其 L0)计划为空,零动作。
    /// `X-Lake-Session-Id` 头池侧不可见:会话键路径与 Router 的家节点不一致
    /// 时,由跟随流量(b)在该节点命中过阈值后收敛。
    pub fn preplace_on_register(
        &mut self,
        model_id: &str,
        revision: &str,
        pool_kind: i32,
        prefix_hashes: &[Vec<u8>],
    ) -> Option<(String, Vec<KvBlockId>)> {
        let anchor = chain_anchor(prefix_hashes)?.to_vec();
        let nodes = self.shard.ready_nodes();
        if nodes.is_empty() {
            return None;
        }
        // 确定性平局:score 取 max,平局取节点 id 较小者(与 Go 冷路径
        // `id < best` 同向——分数相同近乎不发生,但两侧必须同向才同构)。
        let home = pick_home(&nodes, |n| hrw_score(&anchor, n))?;
        let pk = resolve_pool_kind(pool_kind).ok()?;
        let key = NamespaceKey::new(model_id, revision);
        let pool = self.namespaces.get(&key)?.pools.get(&pk)?;

        let mut plan = Vec::new();
        for flat in prefix_hashes {
            let Some(entry) = pool.by_flat.get(flat) else {
                continue;
            };
            let already_l0 = entry
                .meta
                .locations
                .iter()
                .any(|l| l.tier == Tier::L0 as i32 && l.node_id == home);
            if already_l0 {
                continue;
            }
            if pool.placement_marks.contains(&(flat.clone(), home.clone())) {
                continue;
            }
            plan.push(entry.meta.id.clone().unwrap_or_else(|| KvBlockId {
                model_id: model_id.into(),
                revision: revision.into(),
                pool_kind: pk,
                block_hash: flat.clone(),
                scope: "public".into(),
            }));
        }
        if plan.is_empty() {
            return None;
        }
        let pool = self
            .namespaces
            .get_mut(&key)
            .and_then(|ns| ns.pools.get_mut(&pk))
            .expect("checked above");
        for id in &plan {
            pool.placement_marks
                .insert((id.block_hash.clone(), home.clone()));
        }
        Some((home, plan))
    }

    /// (b) 跟随流量评估:本次上报的命中块中,per-(block,node) 计数过阈值、
    /// 未滞回标记且不在该节点 L0 的,取 top-[`FOLLOW_EVAL_LIMIT`] 候选,
    /// 并做**前缀祖先共置**(D-direct 需要整链:放置块 B 时把其在 radix 中
    /// 已注册、未在该节点 L0 的祖先链一并放,根→叶序)。
    ///
    /// 返回 `(目标节点, 放置计划)`;无候选时为空。已下发的 (block,node)
    /// 记入滞回标记,后续命中不重复触发。
    pub(crate) fn follow_traffic_plans(
        &mut self,
        node_id: &str,
        ids: &[KvBlockId],
    ) -> Vec<(String, Vec<KvBlockId>)> {
        if node_id.is_empty() {
            return Vec::new();
        }
        let mut seen = HashSet::new();
        let mut cand: Vec<(NamespaceKey, i32, u32, Vec<u8>)> = Vec::new();
        for id in ids {
            let Ok(pk) = resolve_pool_kind(id.pool_kind) else {
                continue;
            };
            let key = NamespaceKey::from_id(id);
            if !seen.insert((key.clone(), pk, id.block_hash.clone())) {
                continue;
            }
            let Some(pool) = self.namespaces.get(&key).and_then(|ns| ns.pools.get(&pk)) else {
                continue;
            };
            let Some(entry) = pool.by_flat.get(&id.block_hash) else {
                continue;
            };
            let cnt = pool
                .hit_counts
                .get(&id.block_hash)
                .and_then(|m| m.get(node_id))
                .copied()
                .unwrap_or(0);
            if cnt < FOLLOW_HIT_THRESHOLD {
                continue;
            }
            if pool
                .placement_marks
                .contains(&(id.block_hash.clone(), node_id.to_string()))
            {
                continue;
            }
            let already_l0 = entry
                .meta
                .locations
                .iter()
                .any(|l| l.tier == Tier::L0 as i32 && l.node_id == node_id);
            if already_l0 {
                // 已在 L0 的块不标记:未来被驱逐后仍可再触发(自愈)。
                continue;
            }
            cand.push((key, pk, cnt, id.block_hash.clone()));
        }
        cand.sort_by(|a, b| b.2.cmp(&a.2).then_with(|| a.3.cmp(&b.3)));
        cand.truncate(FOLLOW_EVAL_LIMIT);
        if cand.is_empty() {
            return Vec::new();
        }

        // 祖先共置展开(不可变读),再统一写滞回标记。
        let mut planned = HashSet::new();
        let mut out: Vec<KvBlockId> = Vec::new();
        let mut marks: Vec<(NamespaceKey, i32, Vec<u8>)> = Vec::new();
        for (key, pk, _, flat) in &cand {
            let Some(pool) = self.namespaces.get(key).and_then(|ns| ns.pools.get(pk)) else {
                continue;
            };
            let Some(entry) = pool.by_flat.get(flat) else {
                continue;
            };
            for anc in &entry.prefix_chain {
                let Some(e) = pool.by_flat.get(anc) else {
                    continue;
                };
                let already_l0 = e
                    .meta
                    .locations
                    .iter()
                    .any(|l| l.tier == Tier::L0 as i32 && l.node_id == node_id);
                if already_l0 || !planned.insert(anc.clone()) {
                    continue;
                }
                marks.push((key.clone(), *pk, anc.clone()));
                out.push(e.meta.id.clone().unwrap_or_else(|| KvBlockId {
                    model_id: key.model_id.clone(),
                    revision: key.revision.clone(),
                    pool_kind: *pk,
                    block_hash: anc.clone(),
                    scope: "public".into(),
                }));
            }
        }
        for (key, pk, flat) in marks {
            if let Some(pool) = self
                .namespaces
                .get_mut(&key)
                .and_then(|ns| ns.pools.get_mut(&pk))
            {
                pool.placement_marks.insert((flat, node_id.to_string()));
            }
        }
        if out.is_empty() {
            return Vec::new();
        }
        vec![(node_id.to_string(), out)]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// FNV-1a 64 与 Go `hash/fnv` New64a 的锚定向量(跨语言一致性的根)。
    #[test]
    fn fnv1a64_matches_go_reference_vectors() {
        // Go: fnv.New64a(); Write([]byte("")); Sum64()
        assert_eq!(fnv1a64_update(FNV_OFFSET, b""), 14695981039346656037);
        // Go: Write([]byte("a")) → 0xaf63dc4c8601ec8c
        assert_eq!(fnv1a64_update(FNV_OFFSET, b"a"), 0xaf63dc4c8601ec8c);
        // Go: Write([]byte("lake")) → 0x0471d1ad906f0dde(已用 Go fnv 核对)
        assert_eq!(fnv1a64_update(FNV_OFFSET, b"lake"), 0x0471d1ad906f0dde);
    }

    /// 跨语言镜像向量:与 Go `affinity_test.go::TestFnv1a64CrossLanguageVectors`
    /// 钉**同一组** (key,node)→score 绝对值——任一侧改字节布局/字节序,对方
    /// 测试都会报错(方案 Z「冷路径与池侧预放置逐点同家」的回归保险;裸 FNV
    /// 向量只钉原语,钉不住 hrw_score 的布局)。
    #[test]
    fn hrw_score_matches_go_mirror_vectors() {
        assert_eq!(hrw_score(b"anchor-0", "n0"), 0xc8e4e7b063e4ef7f);
        assert_eq!(hrw_score(b"key!", "worker-1"), 0xd485e7e9b3d80a5f);
    }

    #[test]
    fn hrw_score_deterministic_and_node_sensitive() {
        let key = b"anchor-0";
        let a = hrw_score(key, "n0");
        assert_eq!(a, hrw_score(key, "n0"));
        assert_ne!(a, hrw_score(key, "n1"));
        // 长度前缀使 ("ab","c") 与 ("a","bc") 不混淆。
        assert_ne!(hrw_score(b"ab", "c"), hrw_score(b"a", "bc"));
    }

    #[test]
    fn chain_anchor_bounded_depth() {
        assert_eq!(chain_anchor(&[]), None);
        let one = vec![b"h0".to_vec()];
        assert_eq!(chain_anchor(&one), Some(b"h0".as_slice()));
        let ten: Vec<Vec<u8>> = (0..10u8).map(|i| vec![i]).collect();
        assert_eq!(
            chain_anchor(&ten),
            Some(vec![(ANCHOR_DEPTH_CAP - 1) as u8].as_slice())
        );
    }

    /// HRW 最小迁移:加节点时,只有新家节点胜出的键迁移(≈1/N)。
    #[test]
    fn hrw_minimal_migration_on_join() {
        let keys: Vec<Vec<u8>> = (0..200u32).map(|i| format!("k{i}").into_bytes()).collect();
        let s = |ids: &[&str]| ids.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        let (two, three) = (s(&["n0", "n1"]), s(&["n0", "n1", "n2"]));
        let home = |nodes: &[String], key: &[u8]| pick_home(nodes, |n| hrw_score(key, n)).unwrap();
        let mut moved = 0usize;
        for k in &keys {
            let before = home(&two, k);
            let after = home(&three, k);
            if before != after {
                moved += 1;
                assert_eq!(after, "n2", "迁移的键必须落到新节点");
            }
        }
        let ratio = moved as f64 / keys.len() as f64;
        assert!(
            ratio > 0.0 && ratio < 0.55,
            "理想 ≈1/3,宽松上界;ratio={ratio}"
        );
    }

    /// 平局方向:同分取节点 id 较小者,与 Go 冷路径 `id < best` 同向
    /// (review 发现两侧曾反向——Go 取小、Rust max_by 取大,同构声明被证伪)。
    #[test]
    fn pick_home_tie_prefers_smaller_node_id() {
        let nodes = vec!["n1".to_string(), "n0".to_string()];
        assert_eq!(pick_home(&nodes, |_| 42), Some("n0".to_string()));
        assert_eq!(
            pick_home(&nodes, |n| if n == "n1" { 1 } else { 0 }),
            Some("n1".to_string())
        );
    }
}
