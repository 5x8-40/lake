"use strict";

const assert = require("node:assert/strict");
const S = require("./sim.js");

const ONE_MI_TOKEN = 1_048_576;

function close(actual, expected, tolerance, label) {
  assert.ok(
    Math.abs(actual - expected) <= tolerance,
    `${label}: got ${actual}, expected ${expected} ± ${tolerance}`
  );
}

function layout(modelId, profileId, tokens = ONE_MI_TOKEN, includeMtp = false) {
  return S.sessionLayout({ modelId, profileId, tokens, includeMtp });
}

function roundUp(value, alignment) {
  return Math.ceil(value / alignment) * alignment;
}

// Decompose the vLLM blog's 9.62 GiB BF16 paged-KV anchor at 1 Mi-token.
// Continuation state and allocator pages are separate byte classes.
close(
  layout("v4pro", "bf16-logical").growingBytes / S.GiB,
  9.6171875,
  1e-12,
  "V4-Pro BF16 growing KV"
);
const v32Bf16GiB =
  (ONE_MI_TOKEN * 61 * ((512 + 64) * 2 + 128 * 2)) / S.GiB;
close(v32Bf16GiB, 83.875, 1e-12, "V3.2-style BF16 KV");
close(
  v32Bf16GiB / 9.6246337890625,
  8.71461728708225,
  1e-12,
  "V3.2/V4 blog reduction ratio"
);
close(
  layout("v4pro", "bf16-logical", 1_000_000).pagedKvBytes / S.GiB,
  9.179096221923828,
  1e-12,
  "V4-Pro decimal one-million paged KV"
);

// Published/derived paged-KV anchors and continuation state.
close(
  layout("v4pro", "bf16-logical").pagedKvBytes / S.GiB,
  9.6246337890625,
  1e-12,
  "V4-Pro BF16 blog paged KV"
);
close(
  layout("v4flash", "bf16-logical").pagedKvBytes / S.GiB,
  6.7239990234375,
  1e-12,
  "V4-Flash BF16 paged KV"
);
close(
  layout("v4pro", "bf16-logical").compressorStateBytes / S.MiB,
  17.84375,
  1e-12,
  "V4-Pro compressor residual state"
);
close(
  layout("v4flash", "bf16-logical").compressorStateBytes / S.MiB,
  11.640625,
  1e-12,
  "V4-Flash compressor residual state"
);
close(
  layout("v4pro", "bf16-logical").totalBytes / S.GiB,
  9.642059326171875,
  1e-12,
  "V4-Pro transferable session state"
);
close(
  layout("v4flash", "bf16-logical").totalBytes / S.GiB,
  6.7353668212890625,
  1e-12,
  "V4-Flash transferable session state"
);
close(
  layout("glm52", "fp8-logical").totalBytes / S.GiB,
  46.58203125,
  1e-12,
  "GLM-5.2 base FP8 logical"
);
close(
  layout("glm52", "fp8-logical", ONE_MI_TOKEN, true).totalBytes / S.GiB,
  47.2734375,
  1e-12,
  "GLM-5.2 + MTP FP8 logical"
);
close(
  layout("k3", "fp8-vllm-state").stateBytes / S.MiB,
  428.5546875,
  1e-9,
  "Kimi K3 vLLM KDA state"
);
close(
  layout("k3", "fp8-vllm-state").totalBytes / S.GiB,
  13.918510437011719,
  1e-12,
  "Kimi K3 FP8 MLA + vLLM KDA state"
);

// Independent golden formula: this does not call sessionLayout for any
// intermediate term.
const v4ProBf16PagedGolden =
  30 * (ONE_MI_TOKEN / 4) * (1024 + 256) +
  31 * (ONE_MI_TOKEN / 128) * 1024 +
  61 * 128 * 1024;
assert.equal(
  layout("v4pro", "bf16-logical").pagedKvBytes,
  v4ProBf16PagedGolden
);

// V4 emits compressed rows only at complete C4/C128 group boundaries; SWA
// retains only the available suffix for short contexts.
assert.equal(layout("v4pro", "bf16-logical", 1).growingBytes, 0);
assert.equal(layout("v4pro", "bf16-logical", 3).growingBytes, 0);
assert.equal(
  layout("v4pro", "bf16-logical", 4).growingBytes,
  30 * (1024 + 256)
);
assert.equal(
  layout("v4pro", "bf16-logical", 128).growingBytes,
  30 * 32 * (1024 + 256) + 31 * 1024
);
assert.equal(
  layout("v4pro", "bf16-logical", 127).swaStateBytes,
  61 * 127 * 1024
);
assert.equal(
  layout("v4pro", "bf16-logical", 128).swaStateBytes,
  61 * 128 * 1024
);
assert.equal(
  layout("v4pro", "bf16-logical", 129).swaStateBytes,
  61 * 128 * 1024
);
assert.equal(layout("v4pro", "bf16-logical", 0).totalBytes, 0);
assert.equal(layout("k3", "fp8-vllm-state", 0).totalBytes, 0);

// MTP is a real model-specific component: V4 adds one SWA state layer, while
// GLM adds one growing MLA+index layer.
const v4Base = layout("v4pro", "bf16-logical");
const v4Mtp = layout("v4pro", "bf16-logical", ONE_MI_TOKEN, true);
assert.equal(v4Mtp.growingBytes, v4Base.growingBytes);
assert.equal(v4Mtp.swaStateBytes - v4Base.swaStateBytes, 128 * 1024);
const glmBase = layout("glm52", "fp8-logical");
const glmMtp = layout("glm52", "fp8-logical", ONE_MI_TOKEN, true);
assert.equal(
  glmMtp.totalBytes - glmBase.totalBytes,
  ONE_MI_TOKEN * (576 + 132)
);

// Compressor ring policies are separate representations, not a generic
// multiplier. Speculative doubles both rings; online C128 keeps one
// (max,sum,kv) FP32 row.
const v4Spec = S.sessionLayout({
  modelId: "v4pro",
  profileId: "bf16-logical",
  tokens: ONE_MI_TOKEN,
  compressorPolicy: "speculative",
});
assert.equal(v4Spec.compressorStateBytes, 2 * v4Base.compressorStateBytes);
const v4Online = S.sessionLayout({
  modelId: "v4pro",
  profileId: "bf16-logical",
  tokens: ONE_MI_TOKEN,
  compressorPolicy: "online-c128",
});
assert.equal(
  v4Online.compressorStateBytes,
  30 * 8 * ((2 * 2 * 512) + (2 * 2 * 128)) * 4 +
    31 * (3 * 512) * 4
);

// Engine allocation is page/alignment-aware. At a settled snapshot
// (max_in_flight=0), SlidingWindowSpec admits one extra potentially
// misaligned page. The FP4 index profile still allocates 132-byte FP8 rows.
const v4Engine = S.sessionLayout({
  modelId: "v4pro",
  profileId: "fp8-vllm",
  tokens: ONE_MI_TOKEN,
  byteMode: "engine-pages",
});
const fp8GrowingPages =
  (ONE_MI_TOKEN / 256) *
  (30 * (roundUp(64 * 584, 576) + roundUp(64 * 132, 576)) +
    31 * roundUp(2 * 584, 576));
const fp8SwaPages = 61 * 3 * roundUp(64 * 584, 576);
const fp8CompressorPages =
  30 *
    3 *
    (roundUp(4 * (2 * 2 * 512) * 4, 576) +
      roundUp(4 * (2 * 2 * 128) * 4, 576)) +
  31 * 17 * roundUp(8 * (2 * 512) * 4, 576);
assert.equal(v4Engine.selectedGrowingBytes, fp8GrowingPages);
assert.equal(
  v4Engine.selectedTotalBytes,
  fp8GrowingPages + fp8SwaPages + fp8CompressorPages
);
const v4Fp4Engine = S.sessionLayout({
  modelId: "v4pro",
  profileId: "fp8-fp4-payload",
  tokens: ONE_MI_TOKEN,
  byteMode: "engine-pages",
});
assert.equal(v4Fp4Engine.selectedTotalBytes, v4Engine.selectedTotalBytes);
assert.equal(
  S.sessionLayout({
    modelId: "glm52",
    profileId: "fp8-logical",
    tokens: ONE_MI_TOKEN,
    byteMode: "engine-pages",
  }).selectedTotalBytes,
  null
);
close(
  S.sessionLayout({
    modelId: "k3",
    profileId: "fp8-vllm-state",
    tokens: ONE_MI_TOKEN,
    byteMode: "custom-wire",
    customWireFactor: 1.25,
  }).selectedTotalBytes,
  layout("k3", "fp8-vllm-state").totalBytes * 1.25,
  0,
  "custom wire factor"
);

// Prefix matching is block-aligned and never exceeds the context.
assert.equal(S.matchedTokens(1000, 0.5, 256), 256);
assert.equal(S.matchedTokens(1000, 1, 256), 768);
assert.equal(S.matchedTokens(1024, 1, 256), 1024);
assert.equal(S.matchedTokens(12800, 0.58, 256), 7424);
assert.equal(S.matchedTokens(12800, 0.579999, 256), 7168);

const baseTraffic = {
  modelId: "k3",
  profileId: "fp8-vllm-state",
  contextTokens: ONE_MI_TOKEN,
  extraTokens: 0,
  hitRate: 0.99,
  blockTokens: 256,
  remoteFraction: 1,
  admissionFraction: 1,
  writeState: true,
  gpus: 1,
  uniqueTpsGpu: 10_000,
  linkGBsGpu: 0,
  handoffMode: "local",
  loadMode: "compute",
};

// KDA state is transferred once, not double-counted.
const k3 = S.trafficScenario(baseTraffic);
close(
  k3.readPerRequest,
  k3.matchedLayout.growingBytes + k3.matchedLayout.stateBytes,
  0,
  "K3 read state count"
);
close(k3.stateWriteBytes, k3.finalLayout.stateBytes, 0, "K3 write state count");

// A zero hit never reads a fixed state snapshot.
const miss = S.trafficScenario({ ...baseTraffic, hitRate: 0 });
assert.equal(miss.matchedTokens, 0);
assert.equal(miss.readPerRequest, 0);
assert.equal(miss.requiredRead, 0);

// A link budget caps request throughput only when explicitly supplied.
const uncapped = S.trafficScenario(baseTraffic);
assert.equal(uncapped.linkReqCeiling, null);
assert.equal(uncapped.cappedReqsPool, null);
const capped = S.trafficScenario({ ...baseTraffic, linkGBsGpu: 1 });
close(
  capped.linkReqCeiling,
  1e9 / (capped.readPerRequest + capped.writePerRequest),
  1e-12,
  "shared-link ceiling"
);
assert.ok(capped.cappedReqsPool < capped.offeredReqsPool);

// A full hit with no new input has an unknown arrival rate: unique-token
// throughput cannot be used as its denominator. It must never produce NaN.
const fullHit = S.trafficScenario({
  ...baseTraffic,
  contextTokens: 1024,
  hitRate: 1,
  blockTokens: 256,
});
assert.equal(fullHit.uniqueTokens, 0);
assert.equal(fullHit.offeredReqsPool, null);
assert.equal(fullHit.requiredRead, null);
assert.equal(fullHit.requiredWrite, 0);
assert.equal(fullHit.cappedReqsPool, null);
assert.equal(fullHit.cappedRead, null);
assert.equal(fullHit.cappedWrite, 0);
assert.ok(Number.isFinite(fullHit.readPerRequest));

// An external arrival rate is the only defined way to size U=0 offered load.
const fullHitArrival = S.trafficScenario({
  ...fullHit,
  modelId: baseTraffic.modelId,
  profileId: baseTraffic.profileId,
  contextTokens: 1024,
  hitRate: 1,
  blockTokens: 256,
  loadMode: "arrival",
  arrivalReqsPool: 37,
});
assert.equal(fullHitArrival.offeredReqsPool, 37);
close(
  fullHitArrival.requiredRead,
  37 * fullHitArrival.cmxReadPerRequest,
  0,
  "external lambda at U=0"
);

// With q=0 and no writes, bandwidth is exactly zero even though request rate
// cannot be inferred.
const localFullHit = S.trafficScenario({
  ...baseTraffic,
  contextTokens: 1024,
  hitRate: 1,
  blockTokens: 256,
  remoteFraction: 0,
  linkGBsGpu: 1,
});
assert.equal(localFullHit.offeredReqsPool, null);
assert.equal(localFullHit.readPerRequest, 0);
assert.equal(localFullHit.writePerRequest, 0);
assert.equal(localFullHit.requiredRead, 0);
assert.equal(localFullHit.requiredWrite, 0);
assert.equal(localFullHit.linkReqCeiling, Infinity);

// P->D handoff paths conserve the final representation: direct puts it on the
// P-D fabric, via-CMX adds the same bytes to CMX reads, and local adds neither.
const pdCommon = {
  ...baseTraffic,
  contextTokens: 4096,
  extraTokens: 64,
  hitRate: 0.5,
  blockTokens: 256,
  loadMode: "arrival",
  arrivalReqsPool: 2,
};
const pdLocal = S.trafficScenario({ ...pdCommon, handoffMode: "local" });
const pdDirect = S.trafficScenario({ ...pdCommon, handoffMode: "direct" });
const pdViaCmx = S.trafficScenario({ ...pdCommon, handoffMode: "via-cmx" });
assert.equal(pdLocal.directPerRequest, 0);
assert.equal(pdLocal.decodeReadPerRequest, 0);
assert.equal(
  pdDirect.directPerRequest,
  pdDirect.finalLayout.selectedTotalBytes
);
assert.equal(pdDirect.cmxReadPerRequest, pdLocal.cmxReadPerRequest);
assert.equal(
  pdViaCmx.decodeReadPerRequest,
  pdViaCmx.finalLayout.selectedTotalBytes
);
assert.equal(
  pdViaCmx.cmxReadPerRequest,
  pdLocal.cmxReadPerRequest + pdViaCmx.decodeReadPerRequest
);
assert.equal(pdLocal.writePerRequest, pdDirect.writePerRequest);
assert.equal(pdDirect.writePerRequest, pdViaCmx.writePerRequest);
const forcedViaCmx = S.trafficScenario({
  ...pdCommon,
  handoffMode: "via-cmx",
  admissionFraction: 0,
  writeState: false,
});
assert.equal(forcedViaCmx.effectiveAdmissionFraction, 1);
assert.equal(forcedViaCmx.effectiveWriteState, true);
assert.ok(forcedViaCmx.writePerRequest > 0);

// Disabling state writes still writes only newly generated growing KV.
const noStateWrite = S.trafficScenario({
  ...baseTraffic,
  contextTokens: 1024,
  hitRate: 0.5,
  blockTokens: 256,
  writeState: false,
});
assert.equal(noStateWrite.stateWriteBytes, 0);
close(
  noStateWrite.writePerRequest,
  noStateWrite.finalLayout.growingBytes -
    noStateWrite.matchedLayout.growingBytes,
  0,
  "no-state write"
);

// Fractional residency/admission factors scale bytes exactly once.
const fractional = S.trafficScenario({
  ...baseTraffic,
  contextTokens: 1024,
  hitRate: 0.5,
  blockTokens: 256,
  remoteFraction: 0.25,
  admissionFraction: 0.4,
});
close(
  fractional.readPerRequest,
  0.25 * fractional.matchedLayout.totalBytes,
  0,
  "fractional remote read"
);
close(
  fractional.stateWriteBytes,
  0.4 * fractional.finalLayout.stateBytes,
  0,
  "fractional state admission"
);
close(
  fractional.writePerRequest,
  0.4 *
    (fractional.finalLayout.growingBytes -
      fractional.matchedLayout.growingBytes +
      fractional.finalLayout.stateBytes),
  0,
  "fractional write admission"
);

// Full-duplex budgets cap on the tighter direction, not read+write.
const fullDuplex = S.trafficScenario({
  ...baseTraffic,
  linkGBsGpu: 1,
  linkMode: "full-duplex",
});
close(
  fullDuplex.linkReqCeiling,
  Math.min(
    1e9 / fullDuplex.readPerRequest,
    1e9 / fullDuplex.writePerRequest
  ),
  1e-12,
  "full-duplex ceiling"
);

// A zero-byte full-duplex direction does not cap the other direction.
const readOnlyFullDuplex = S.trafficScenario({
  ...baseTraffic,
  contextTokens: 1024,
  hitRate: 1,
  blockTokens: 256,
  remoteFraction: 1,
  linkGBsGpu: 1,
  linkMode: "full-duplex",
});
close(
  readOnlyFullDuplex.linkReqCeiling,
  1e9 / readOnlyFullDuplex.readPerRequest,
  1e-12,
  "read-only full-duplex ceiling"
);
const writeOnlyFullDuplex = S.trafficScenario({
  ...baseTraffic,
  contextTokens: 1024,
  hitRate: 0,
  remoteFraction: 0,
  linkGBsGpu: 1,
  linkMode: "full-duplex",
});
close(
  writeOnlyFullDuplex.linkReqCeiling,
  1e9 / writeOnlyFullDuplex.writePerRequest,
  1e-12,
  "write-only full-duplex ceiling"
);

// Explicit zeros stay zero in the capacity model.
const zeroCapacity = S.capacityScenario({
  modelId: "v4pro",
  profileId: "bf16-logical",
  tokens: ONE_MI_TOKEN,
  usableHbmGB: 0,
  usableG2TB: 0,
  rawCmxPB: 0,
  cmxUsableFraction: 0,
});
assert.equal(zeroCapacity.hbmSessionsPerGpu, 0);
assert.equal(zeroCapacity.g2SessionsPerRack, 0);
assert.equal(zeroCapacity.usableCmxSessionsPerPod, 0);

// A genuinely zero-byte layout is undefined for capacity division.
const emptyCapacity = S.capacityScenario({
  modelId: "glm52",
  profileId: "fp8-logical",
  tokens: 0,
  usableHbmGB: 100,
  usableG2TB: 1,
  rawCmxPB: 1,
  cmxUsableFraction: 1,
});
assert.equal(emptyCapacity.hbmSessionsPerGpu, null);
assert.equal(emptyCapacity.g2SessionsPerRack, null);
assert.equal(emptyCapacity.usableCmxSessionsPerPod, null);

// Positive session sizes retain exact floor-division semantics.
const positiveCapacity = S.capacityScenario({
  modelId: "glm52",
  profileId: "fp8-logical",
  tokens: 1024,
  usableHbmGB: 1,
  usableG2TB: 0,
  rawCmxPB: 0,
  cmxUsableFraction: 0,
});
assert.equal(
  positiveCapacity.hbmSessionsPerGpu,
  Math.floor(1e9 / positiveCapacity.layout.totalBytes)
);

// HBM sharding and pool replication have different denominators. Fixed KDA
// state can stay replicated while growing MLA is sharded.
const shardedCapacity = S.capacityScenario({
  modelId: "k3",
  profileId: "fp8-vllm-state",
  tokens: ONE_MI_TOKEN,
  usableHbmGB: 80,
  usableG2TB: 1,
  rawCmxPB: 1,
  cmxUsableFraction: 0.8,
  growingShardRanks: 8,
  stateShardRanks: 1,
  poolCopies: 2,
  capacityUnitMode: "binary",
});
close(
  shardedCapacity.hbmSessionBytes,
  shardedCapacity.layout.selectedGrowingBytes / 8 +
    shardedCapacity.layout.selectedStateBytes,
  0,
  "HBM sharded growing plus replicated state"
);
assert.equal(
  shardedCapacity.poolSessionBytes,
  2 * shardedCapacity.layout.selectedTotalBytes
);
assert.equal(
  shardedCapacity.hbmSessionsPerGpu,
  Math.floor(80 * S.GiB / shardedCapacity.hbmSessionBytes)
);
assert.equal(
  shardedCapacity.g2SessionsPerRack,
  Math.floor(1024 ** 4 / shardedCapacity.poolSessionBytes)
);

// Hit comparison keeps Prefill GPUs saturated. Storage consists of one hot
// matched prefix per active session plus admitted writes retained for a
// user-supplied time window.
const comparison = S.hitComparisonScenario({
  modelId: "v4pro",
  profileId: "bf16-logical",
  contextTokens: ONE_MI_TOKEN,
  extraTokens: 0,
  blockTokens: 256,
  remoteFraction: 1,
  admissionFraction: 1,
  writeState: true,
  handoffMode: "direct",
  gpus: 72,
  uniqueTpsGpu: 10_000,
  hitRateLow: 0.9,
  hitRateHigh: 0.95,
  activeSessions: 10_000,
  retentionSeconds: 600,
  poolCopies: 1,
  usableFraction: 0.8,
});
assert.equal(comparison.low.traffic.matchedTokens, 943_616);
assert.equal(comparison.high.traffic.matchedTokens, 996_096);
assert.equal(comparison.low.traffic.uniqueTokens, 104_960);
assert.equal(comparison.high.traffic.uniqueTokens, 52_480);
close(
  comparison.low.traffic.offeredReqsPool,
  6.859756097560975,
  1e-12,
  "90% compute-constrained request rate"
);
close(
  comparison.high.traffic.offeredReqsPool,
  13.71951219512195,
  1e-12,
  "95% compute-constrained request rate"
);
close(
  comparison.low.rawStorageBytes / S.TiB,
  110.91130606761973,
  1e-12,
  "90% raw storage"
);
close(
  comparison.high.rawStorageBytes / S.TiB,
  116.91186568563485,
  1e-12,
  "95% raw storage"
);
close(
  comparison.storageCostIncrease,
  0.054102325820207575,
  1e-12,
  "storage capacity cost increase"
);
assert.equal(comparison.computeReqIncrease, 1);
close(
  comparison.readIncrease,
  1.110912940231262,
  1e-12,
  "read bandwidth increase"
);
close(
  comparison.writeIncrease,
  0.025185900531144334,
  1e-12,
  "write bandwidth increase"
);

const zeroRetention = S.hitComparisonScenario({
  modelId: "v4pro",
  profileId: "bf16-logical",
  contextTokens: ONE_MI_TOKEN,
  blockTokens: 256,
  remoteFraction: 1,
  admissionFraction: 1,
  gpus: 1,
  uniqueTpsGpu: 1,
  hitRateLow: 0.9,
  hitRateHigh: 0.95,
  activeSessions: 1,
  retentionSeconds: 0,
  poolCopies: 2,
  usableFraction: 1,
});
assert.equal(
  zeroRetention.low.rawStorageBytes,
  2 * zeroRetention.low.hotPrefixBytes
);

const unknownUsable = S.hitComparisonScenario({
  modelId: "v4pro",
  profileId: "bf16-logical",
  contextTokens: ONE_MI_TOKEN,
  blockTokens: 256,
  gpus: 1,
  uniqueTpsGpu: 1,
  hitRateLow: 0.9,
  hitRateHigh: 0.95,
  activeSessions: 1,
  retentionSeconds: 0,
  usableFraction: 0,
});
assert.equal(unknownUsable.low.rawStorageBytes, null);

// Formatter edge cases distinguish unknown/invalid from an unbounded ceiling.
assert.equal(S.fmtGiB(null), "N/A");
assert.equal(S.fmtGiB(NaN), "N/A");
assert.equal(S.fmtGBs(NaN), "N/A");
assert.equal(S.fmtGBs(63.929059902439015 * S.GB), "63.93 GB/s");
assert.equal(S.fmtCapacity(S.TiB), "1.00 TiB");
assert.equal(S.fmtRate(NaN), "N/A");
assert.equal(S.fmtRate(Infinity), "∞");

console.log("ok");
console.log("  V4-Pro growing KV     =", S.fmtGiB(layout("v4pro", "bf16-logical").growingBytes));
console.log("  V4-Pro blog paged KV  =", S.fmtGiB(layout("v4pro", "bf16-logical").pagedKvBytes));
console.log("  V4-Pro + comp. state  =", S.fmtGiB(layout("v4pro", "bf16-logical").totalBytes));
console.log("  GLM-5.2 base FP8      =", S.fmtGiB(layout("glm52", "fp8-logical").totalBytes));
console.log("  Kimi K3 FP8 + state   =", S.fmtGiB(layout("k3", "fp8-vllm-state").totalBytes));
