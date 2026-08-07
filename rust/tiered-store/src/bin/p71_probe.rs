//! P7.1 测量基座:Rust 探针。
//! 跑:`LAKE_BENCH_OUT=bench/results/rust.jsonl cargo run -p lake-tiered-store --bin p71_probe`
//! 采集 put_durable / promote_to_l0 时延分布 + HitStats 分层命中计数,
//! JSONL schema 见 bench/README.md。原型相对值(内存层模拟),真介质校准 defer(issue #61)。

use lake_tiered_store::{LocalTierEngine, TierCaps};
use std::io::Write;
use std::time::Instant;

fn percentile(mut v: Vec<f64>, q: f64) -> f64 {
    if v.is_empty() {
        return 0.0;
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let idx = (q * (v.len() - 1) as f64) as usize;
    v[idx]
}

fn emit(name: &str, lat_ms: &[(&str, f64)], counters: &[(&str, u64)]) {
    let Some(out) = std::env::var_os("LAKE_BENCH_OUT") else {
        return;
    };
    let lat: String = lat_ms
        .iter()
        .map(|(k, v)| format!("\"{k}\":{v:.3}"))
        .collect::<Vec<_>>()
        .join(",");
    let cnt: String = counters
        .iter()
        .map(|(k, v)| format!("\"{k}\":{v}"))
        .collect::<Vec<_>>()
        .join(",");
    let line = format!(
        "{{\"name\":\"{name}\",\"lang\":\"rust\",\"phase\":\"P7.1\",\
         \"env\":{{\"compute\":\"mock-bytes\",\"tiers\":\"in-mem L0/L1/L2\",\"note\":\"原型相对值;真介质校准 defer(issue #61)\"}},\
         \"latency_ms\":{{{lat}}},\"counters\":{{{cnt}}}}}\n"
    );
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(out)
        .expect("open LAKE_BENCH_OUT");
    f.write_all(line.as_bytes()).expect("write jsonl");
}

fn main() {
    const N: u32 = 512;
    let mut e = LocalTierEngine::with_caps(TierCaps {
        l0: 64, // 故意小容量:制造 L1/L2 命中与 promote
        l1: 256,
        l2: 1024,
    });
    let block = vec![0u8; 4096]; // 4KiB mock block

    // 先灌满分层(超过 L0 容量 → 驱逐到 L1/L2)
    for i in 0..N {
        e.put_durable(&i.to_le_bytes(), &block).unwrap();
    }

    // 探针 1:promote(L1/L2 → L0)时延(含驱逐路径:promote_to_l0 内部
    // ensure_l0_room 先驱逐再 insert,故无需容量守卫——review #63)
    let mut promote_ms = Vec::new();
    for i in 0..N {
        let h = i.to_le_bytes();
        let t = Instant::now();
        if e.promote_to_l0(&h).is_ok() {
            promote_ms.push(t.elapsed().as_secs_f64() * 1000.0);
        }
    }

    // 探针 2:put_durable 时延(含驱逐路径)
    let mut put_ms = Vec::new();
    for i in N..2 * N {
        let t = Instant::now();
        e.put_durable(&i.to_le_bytes(), &block).unwrap();
        put_ms.push(t.elapsed().as_secs_f64() * 1000.0);
    }

    // 探针 3:probe 命中分布(L0/L1/L2/miss 各多少,记入 HitStats)
    for i in 0..2 * N {
        e.probe(&i.to_le_bytes()); // 前一半在层内,后一半 miss
    }

    let s = &e.stats;
    emit(
        "tier_promote_l0",
        &[
            ("p50", percentile(promote_ms.clone(), 0.50)),
            ("p99", percentile(promote_ms.clone(), 0.99)),
        ],
        &[("samples", promote_ms.len() as u64)],
    );
    emit(
        "tier_put_durable",
        &[
            ("p50", percentile(put_ms.clone(), 0.50)),
            ("p99", percentile(put_ms.clone(), 0.99)),
        ],
        &[("samples", put_ms.len() as u64)],
    );
    emit(
        "tier_hit_stats",
        &[],
        &[
            ("l0", s.l0),
            ("l1", s.l1),
            ("l2", s.l2),
            ("l3", s.l3),
            ("miss", s.miss),
            ("total_cost", s.total_cost),
        ],
    );
    eprintln!(
        "promote p50={:.3}ms p99={:.3}ms; put p50={:.3}ms p99={:.3}ms; stats l0={} l1={} l2={} miss={}",
        percentile(promote_ms.clone(), 0.50),
        percentile(promote_ms, 0.99),
        percentile(put_ms.clone(), 0.50),
        percentile(put_ms, 0.99),
        s.l0,
        s.l1,
        s.l2,
        s.miss,
    );
}
