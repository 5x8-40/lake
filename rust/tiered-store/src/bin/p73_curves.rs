//! P7.3 分层缓存命中率/成本曲线。
//! 跑:`LAKE_BENCH_OUT=bench/results/rust-p73.jsonl cargo run -p lake-tiered-store --bin p73_curves`
//!
//! 合成 workload(zipf 热度 + 可调前缀共享度)驱动 LocalTierEngine,产出:
//!   1. hit_curve          L0 容量 → L0/L1/L2/miss 命中率分解
//!   2. promote_calibration  promote 实测时延 vs estimate_promote_cost(hops 模型)
//!   3. block_granularity    block {64,128,256} 的量化损失与有效复用率
//!   4. writeback_scan       decode 写回批量 N ∈ {1,2,4}:ops 与字节量关系
//!   5. gc_proxy             迁移字节 / 访问字节(GC/整理带宽占比代理)
//!
//! 引擎为内存模拟层 → 命中率/比例类结论与介质无关;时延类为原型相对值。
//! 真介质带宽绝对值 defer(issue #61)。

use lake_tiered_store::{AccessKind, LocalTierEngine, TierCaps};
use std::io::Write;
use std::time::Instant;

// ---- xorshift64 RNG(无新依赖) ----
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
    fn chance(&mut self, pct: u64) -> bool {
        self.below(100) < pct
    }
}

// ---- zipf 采样:权重 1/i^alpha 预计算累积表,二分取样 ----
struct Zipf {
    cum: Vec<f64>,
    total: f64,
}
impl Zipf {
    fn new(n: usize, alpha: f64) -> Self {
        let mut cum = Vec::with_capacity(n);
        let mut acc = 0.0;
        for i in 1..=n {
            acc += 1.0 / (i as f64).powf(alpha);
            cum.push(acc);
        }
        let total = *cum.last().unwrap();
        Zipf { cum, total }
    }
    fn sample(&self, rng: &mut Rng) -> usize {
        let u = (rng.next() as f64) / (u64::MAX as f64) * self.total;
        self.cum.partition_point(|&c| c < u)
    }
}

const BLOCK_BYTES: usize = 4096; // mock 块字节;命中率与字节无关,带宽代理按比例换算
const HOT_PREFIXES: usize = 256; // 共享热前缀池大小
const UNIQUE_PREFIXES: usize = 4096; // 独立前缀池
const BLOCKS_PER_REQ: usize = 16; // 每请求块数(=2048 token @128)

/// 第 seq 个请求的第 i 块哈希:share_pct 概率取共享热前缀(zipf 选热度),
/// 否则取独立前缀。哈希 = 前缀 id ‖ 块序号,保证同前缀同块同哈希。
fn gen_request(rng: &mut Rng, zipf: &Zipf, share_pct: u64) -> Vec<u64> {
    let prefix: u64 = if rng.chance(share_pct) {
        zipf.sample(rng) as u64
    } else {
        HOT_PREFIXES as u64 + rng.below(UNIQUE_PREFIXES as u64)
    };
    (0..BLOCKS_PER_REQ as u64)
        .map(|i| (prefix << 32) | i)
        .collect()
}

/// 驱动一轮 workload;返回 (l0/l1/l2 命中, l3 命中, 冷 miss, 写字节, 迁移字节, promote 次数)。
/// L3 命中与冷 miss 分开记(review #65):doc 的「miss 率」= l3+冷 miss(保守口径,
/// L3 fetch 慢视同 miss),但数据上两者可分。
fn drive(
    caps: TierCaps,
    n_requests: usize,
    share_pct: u64,
    seed: u64,
) -> (u64, u64, u64, u64, u64, u64, u64, u64) {
    let mut e = LocalTierEngine::with_caps(caps);
    let mut rng = Rng(seed | 1);
    let zipf = Zipf::new(HOT_PREFIXES, 1.2);
    let block = vec![0u8; BLOCK_BYTES];
    let (mut write_bytes, mut moved_bytes, mut promotes) = (0u64, 0u64, 0u64);
    for _ in 0..n_requests {
        for h in gen_request(&mut rng, &zipf, share_pct) {
            let hb = h.to_le_bytes();
            match e.probe(&hb) {
                AccessKind::L0Hit => {}
                AccessKind::Miss => {
                    let (_, fx) = e.put_durable(&hb, &block).unwrap();
                    write_bytes += BLOCK_BYTES as u64;
                    moved_bytes += (fx.l0_demoted.len() + fx.l2_demoted_to_l3.len()) as u64
                        * BLOCK_BYTES as u64;
                }
                _ => {
                    // L1/L2/L3 命中:promote 回 L0(读 miss 回填,被动兜底)
                    if let Ok((_, fx)) = e.promote_to_l0(&hb) {
                        promotes += 1;
                        moved_bytes += BLOCK_BYTES as u64
                            + (fx.l0_demoted.len() + fx.l2_demoted_to_l3.len()) as u64
                                * BLOCK_BYTES as u64;
                    }
                }
            }
        }
    }
    let s = &e.stats;
    (
        s.l0,
        s.l1,
        s.l2,
        s.l3,
        s.miss,
        write_bytes,
        moved_bytes,
        promotes,
    )
}

/// 带 promote 频率准入的对照驱动(决策 B 真实机制):非 L0 命中走引擎一等 API
/// `promote_to_l0_admitted`——hit_count ≥ admit_after 给热块待遇,否则 one-shot
/// (照样进 L0,GPU 约束下不存在"不搬直读";但驱逐最优先、不挤热块,砍级联)。
/// 返回同 drive()。
fn drive_admitted(
    caps: TierCaps,
    n_requests: usize,
    share_pct: u64,
    seed: u64,
    admit_after: u32,
) -> (u64, u64, u64, u64, u64, u64, u64, u64) {
    let mut e = LocalTierEngine::with_caps(caps).with_promote_admit_after(admit_after);
    let mut rng = Rng(seed | 1);
    let zipf = Zipf::new(HOT_PREFIXES, 1.2);
    let block = vec![0u8; BLOCK_BYTES];
    let (mut write_bytes, mut moved_bytes, mut promotes) = (0u64, 0u64, 0u64);
    for _ in 0..n_requests {
        for h in gen_request(&mut rng, &zipf, share_pct) {
            let hb = h.to_le_bytes();
            match e.probe(&hb) {
                AccessKind::L0Hit => {}
                AccessKind::Miss => {
                    let (_, fx) = e.put_durable(&hb, &block).unwrap();
                    write_bytes += BLOCK_BYTES as u64;
                    moved_bytes += (fx.l0_demoted.len() + fx.l2_demoted_to_l3.len()) as u64
                        * BLOCK_BYTES as u64;
                }
                _ => {
                    if let Ok((_, fx)) = e.promote_to_l0_admitted(&hb) {
                        promotes += 1;
                        moved_bytes += BLOCK_BYTES as u64
                            + (fx.l0_demoted.len() + fx.l2_demoted_to_l3.len()) as u64
                                * BLOCK_BYTES as u64;
                    }
                }
            }
        }
    }
    let s = &e.stats;
    (
        s.l0,
        s.l1,
        s.l2,
        s.l3,
        s.miss,
        write_bytes,
        moved_bytes,
        promotes,
    )
}

fn emit(name: &str, counters: &[(&str, u64)]) {
    let Some(out) = std::env::var_os("LAKE_BENCH_OUT") else {
        return;
    };
    let cnt: String = counters
        .iter()
        .map(|(k, v)| format!("\"{k}\":{v}"))
        .collect::<Vec<_>>()
        .join(",");
    let line = format!(
        "{{\"name\":\"{name}\",\"lang\":\"rust\",\"phase\":\"P7.3\",\
         \"env\":{{\"workload\":\"zipf1.2 share-driven\",\"tiers\":\"in-mem sim\",\"note\":\"比例类结论介质无关;时延为原型相对值\"}},\
         \"counters\":{{{cnt}}}}}\n"
    );
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(out)
        .expect("open");
    f.write_all(line.as_bytes()).expect("write");
}

fn main() {
    const REQUESTS: usize = 3000;
    const SHARE_PCT: u64 = 70; // 70% 请求走共享热前缀(公共前缀场景)

    // 1. 命中率-容量曲线(miss 列 = L3 命中 + 冷 miss,保守口径;emit 里两者分开)
    eprintln!("== hit_curve (requests={REQUESTS}, share={SHARE_PCT}%, miss含L3) ==");
    for l0 in [32u64, 64, 128, 256, 512] {
        let caps = TierCaps {
            l0: l0 as usize,
            l1: 4 * l0 as usize,
            l2: 16 * l0 as usize,
        };
        let (h0, h1, h2, h3, miss, wb, mb, pr) = drive(caps, REQUESTS, SHARE_PCT, 42);
        let total = (h0 + h1 + h2 + h3 + miss) as f64;
        eprintln!(
            "  L0 cap {l0:>4}: hit={:.1}% (l0 {:.1}/l1 {:.1}/l2 {:.1}) miss={:.1}%(含L3 {:.1}%) moved/write={:.2}",
            100.0 * (h0 + h1 + h2) as f64 / total,
            100.0 * h0 as f64 / total,
            100.0 * h1 as f64 / total,
            100.0 * h2 as f64 / total,
            100.0 * (h3 + miss) as f64 / total,
            100.0 * h3 as f64 / total,
            mb as f64 / wb.max(1) as f64,
        );
        emit(
            "hit_curve",
            &[
                ("l0_cap", l0),
                ("l0_hit", h0),
                ("l1_hit", h1),
                ("l2_hit", h2),
                ("l3_hit", h3),
                ("cold_miss", miss),
                ("write_bytes", wb),
                ("moved_bytes", mb),
                ("promotes", pr),
            ],
        );
    }

    // 2. promote cost 校准:hops 模型(estimate=nbytes×hops) vs 实测。
    // put_durable 落 L2(durable 层),L0/L1 只能经 promote 填充——
    // 构造:A 促到 L0 后被 B 挤到 L1(hops=1);D1 留 L2(hops=2);
    // E 灌满 L2 把 D2 逐到 L3(hops=3)。
    eprintln!("== promote_calibration ==");
    let block = vec![0u8; BLOCK_BYTES];
    let mut e = LocalTierEngine::with_caps(TierCaps {
        l0: 8,
        l1: 64,
        l2: 512,
    });
    let id = |tag: u64, i: u64| ((tag << 32) | i).to_le_bytes();
    let mut per_tier: std::collections::BTreeMap<u64, Vec<f64>> = Default::default();
    macro_rules! measure {
        ($tag:expr, $i:expr) => {{
            let h = id($tag, $i);
            let hops = e.estimate_promote_cost(&h) / BLOCK_BYTES as u64;
            let t = Instant::now();
            if e.promote_to_l0(&h).is_ok() {
                per_tier
                    .entry(hops)
                    .or_default()
                    .push(t.elapsed().as_secs_f64() * 1e6);
            }
        }};
    }
    for i in 0..8 {
        e.put_durable(&id(1, i), &block).unwrap(); // A
        e.put_durable(&id(2, i), &block).unwrap(); // B
    }
    for i in 0..16 {
        e.put_durable(&id(3, i), &block).unwrap(); // D1(hops=2 测量组)
        e.put_durable(&id(4, i), &block).unwrap(); // D2(hops=3 测量组)
    }
    for i in 0..8 {
        e.promote_to_l0(&id(1, i)).unwrap(); // A 占满 L0
    }
    for i in 0..8 {
        e.promote_to_l0(&id(2, i)).unwrap(); // B 把 A 挤到 L1
    }
    for i in 0..8 {
        measure!(1, i); // A 在 L1 → hops=1
    }
    for i in 0..16 {
        measure!(3, i); // D1 在 L2(未经 L1)→ hops=2
    }
    for i in 0..600 {
        e.put_durable(&id(5, i), &block).unwrap(); // E 灌满 L2,D2 逐到 L3
    }
    for i in 0..16 {
        measure!(4, i); // D2 在 L3 → hops=3
    }
    // hops=1/2 在 mock 内存层同为 HashMap 操作,差异低于噪声底(不可区分);
    // 稳健口径 = hops3/hops1 比值(L3 源的固定开销真实可测)。review #65。
    let mut ratio = 0.0;
    let mut h1_avg = 0.0;
    for (hops, v) in &per_tier {
        let avg = v.iter().sum::<f64>() / v.len() as f64;
        eprintln!("  hops={hops}: avg {avg:.2}µs (n={})", v.len());
        emit(
            "promote_calibration",
            &[
                ("hops", *hops),
                ("avg_us_x1000", (avg * 1000.0) as u64),
                ("samples", v.len() as u64),
            ],
        );
        if *hops == 1 {
            h1_avg = avg;
        } else if *hops == 3 {
            ratio = avg / h1_avg.max(1e-9);
        }
    }
    eprintln!("  hops3/hops1 = {ratio:.1}×(稳健口径;hops1≈hops2 为噪声)");
    emit(
        "promote_calibration_ratio",
        &[("hops3_over_hops1_x100", (ratio * 100.0) as u64)],
    );

    // 3. block 粒度:共享前缀在非对齐长度下的有效复用率
    eprintln!("== block_granularity ==");
    for blk in [64u64, 128, 256] {
        // prompt 长度均匀 [256, 2048],共享前缀整段命中;复用率 = ⌊hit/blk⌋·blk / prompt
        let mut rng = Rng(7);
        let (mut reused, mut total) = (0u64, 0u64);
        for _ in 0..10000 {
            let prompt = 256 + rng.below(1793); // [256,2048]
            let hit = prompt; // 整段前缀复用(上限情形)
            reused += hit / blk * blk;
            total += prompt;
        }
        let ratio = reused as f64 / total as f64;
        eprintln!("  block={blk:>3}: 有效复用率 {:.2}%", ratio * 100.0);
        emit(
            "block_granularity",
            &[
                ("block_tokens", blk),
                ("reuse_ratio_x10000", (ratio * 10000.0) as u64),
            ],
        );
    }

    // 4. decode 写回批量 N:字节不变,ops ∝ 1/N
    eprintln!("== writeback_scan ==");
    const DECODE_BLOCKS: u64 = 8; // 每请求 decode 产出 8 块(1024 token @128)
    for n in [1u64, 2, 4] {
        let ops = DECODE_BLOCKS.div_ceil(n) * REQUESTS as u64;
        let bytes = DECODE_BLOCKS * BLOCK_BYTES as u64 * REQUESTS as u64;
        eprintln!("  batch N={n}: ops={ops} bytes={bytes} (bytes 与 N 无关)");
        emit(
            "writeback_scan",
            &[("batch_n", n), ("ops", ops), ("bytes", bytes)],
        );
    }

    // 5b. promote 频率准入(hit_count≥2)vs 无准入对照:churn 降幅 / 命中率与 L0 份额代价。
    eprintln!("== admission_experiment ==");
    for l0 in [64u64, 128, 512] {
        let caps = TierCaps {
            l0: l0 as usize,
            l1: 4 * l0 as usize,
            l2: 16 * l0 as usize,
        };
        let base = drive(caps, REQUESTS, SHARE_PCT, 42);
        let adm = drive_admitted(caps, REQUESTS, SHARE_PCT, 42, 2);
        for (tag, r) in [("baseline", base), ("admit>=2", adm)] {
            let (h0, h1, h2, h3, miss, wb, mb, pr) = r;
            let total = (h0 + h1 + h2 + h3 + miss) as f64;
            eprintln!(
                "  L0={l0:>4} {tag:<9}: hit={:.1}%(l0 {:.1}%) miss={:.1}%(含L3 {:.1}%) moved/write={:.2} moved/(r+w)={:.0}% promotes={}",
                100.0 * (h0 + h1 + h2) as f64 / total,
                100.0 * h0 as f64 / total,
                100.0 * (h3 + miss) as f64 / total,
                100.0 * h3 as f64 / total,
                mb as f64 / wb.max(1) as f64,
                100.0 * mb as f64 / ((h0 + h1 + h2 + h3) * BLOCK_BYTES as u64 + wb) as f64,
                pr,
            );
            emit(
                "admission_experiment",
                &[
                    ("l0_cap", l0),
                    ("admitted", u64::from(tag != "baseline")),
                    ("l0_hit", h0),
                    ("l1_hit", h1),
                    ("l2_hit", h2),
                    ("l3_hit", h3),
                    ("cold_miss", miss),
                    ("write_bytes", wb),
                    ("moved_bytes", mb),
                    ("promotes", pr),
                ],
            );
        }
    }

    // 5. 同步迁移放大(写驱逐+读回填,数据路径,≠后台 GC/defrag):
    //    后台 <10% 由 BandwidthPool::default_throttle 构造保证(10% link/1s 窗,可暂停)。
    let (h0, h1, h2, h3, _miss, wb, mb, _) = drive(TierCaps::default(), REQUESTS, SHARE_PCT, 99);
    let read_bytes = (h0 + h1 + h2 + h3) * BLOCK_BYTES as u64;
    let share = mb as f64 / (read_bytes + wb) as f64;
    eprintln!("== sync_migration_proxy(数据路径,非后台带宽池)==\n  moved/(read+write) = {:.2}% (moved={mb}, read={read_bytes}, write={wb})", share * 100.0);
    emit(
        "gc_proxy",
        &[
            ("moved_bytes", mb),
            ("read_bytes", read_bytes),
            ("write_bytes", wb),
            ("share_x10000", (share * 10000.0) as u64),
        ],
    );
}
