//! P4.3 吞吐 micro-benchmark（criterion）。
//! 跑：`cd rust && cargo bench -p lake-tiered-store --bench tier_access`

use criterion::{black_box, criterion_group, criterion_main, Criterion};
use lake_tiered_store::{LocalTierEngine, TierCaps};

fn bench_put_and_promote(c: &mut Criterion) {
    c.bench_function("put_durable_promote_l0", |b| {
        b.iter(|| {
            let mut e = LocalTierEngine::with_caps(TierCaps {
                l0: 1024,
                l1: 2048,
                l2: 4096,
            });
            for i in 0..64u32 {
                let h = i.to_le_bytes();
                e.put_durable(black_box(&h), black_box(b"0123456789abcdef"))
                    .unwrap();
                let _ = e.promote_to_l0(black_box(&h)).unwrap();
            }
        })
    });
}

fn bench_probe_mix(c: &mut Criterion) {
    let mut e = LocalTierEngine::with_caps(TierCaps::default());
    for i in 0..32u32 {
        let h = i.to_le_bytes();
        e.put_durable(&h, b"x").unwrap();
        if i % 4 == 0 {
            let _ = e.demote_l2_to_l3(&h);
        }
        if i % 2 == 0 {
            let _ = e.promote_to_l0(&h).unwrap();
        }
    }
    c.bench_function("probe_mixed_tiers", |b| {
        b.iter(|| {
            for i in 0..32u32 {
                let h = i.to_le_bytes();
                black_box(e.probe(black_box(&h)));
            }
        })
    });
}

criterion_group!(benches, bench_put_and_promote, bench_probe_mix);
criterion_main!(benches);
