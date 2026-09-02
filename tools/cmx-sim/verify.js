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
close(
  layout("glm53flash", "bf16-logical").growingBytes / S.GiB,
  11.6875,
  1e-12,
  "GLM-5.3-Flash BF16 growing KV + indexer"
);
close(
  layout("glm53flash", "bf16-logical").stateBytes / S.MiB,
  140.78125,
  1e-9,
  "GLM-5.3-Flash vLLM KDA state"
);
close(
  layout("glm53flash", "bf16-logical").totalBytes / S.GiB,
  11.824981689453125,
  1e-12,
  "GLM-5.3-Flash transferable session state"
);
close(
  layout("glm53flash", "fp8-logical").growingBytes / S.GiB,
  5.84375,
  1e-12,
  "GLM-5.3-Flash FP8 growing KV + indexer"
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
// GLM-5.3-Flash indexer entries likewise materialize per closed kpool=4
// group, while sparse-MLA KV grows per token.
assert.equal(
  layout("glm53flash", "bf16-logical", 3).growingBytes,
  3 * 11 * 1024
);
assert.equal(
  layout("glm53flash", "bf16-logical", 4).growingBytes,
  4 * 11 * 1024 + 11 * 256
);
assert.equal(layout("glm53flash", "bf16-logical", 0).totalBytes, 0);
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
// GLM-5.3-Flash MTP adds one sparse-MLA KV layer and shares the indexer
// (index_share_for_mtp_iteration), so the delta carries no index entry.
const glm53fBase = layout("glm53flash", "bf16-logical");
const glm53fMtp = layout("glm53flash", "bf16-logical", ONE_MI_TOKEN, true);
assert.equal(
  glm53fMtp.growingBytes - glm53fBase.growingBytes,
  ONE_MI_TOKEN * 1024
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
  readBudgetGBsPool: 0,
  loadMode: "compute",
};

// KDA state is transferred once, not double-counted.
const k3 = S.prefillScenario(baseTraffic);
close(
  k3.readPerRequest,
  k3.matchedLayout.growingBytes + k3.matchedLayout.stateBytes,
  0,
  "K3 read state count"
);
close(k3.stateWriteBytes, k3.finalLayout.stateBytes, 0, "K3 write state count");

// A zero hit never reads a fixed state snapshot.
const miss = S.prefillScenario({ ...baseTraffic, hitRate: 0 });
assert.equal(miss.matchedTokens, 0);
assert.equal(miss.readPerRequest, 0);
assert.equal(miss.requiredRead, 0);

// A link budget caps request throughput only when explicitly supplied.
const uncapped = S.prefillScenario(baseTraffic);
assert.equal(uncapped.readReqCeiling, null);
assert.equal(uncapped.cappedReqsPool, null);
const capped = S.prefillScenario({ ...baseTraffic, readBudgetGBsPool: 1 });
close(
  capped.readReqCeiling,
  1e9 / capped.readPerRequest,
  1e-12,
  "storage-read ceiling"
);
assert.ok(capped.cappedReqsPool < capped.offeredReqsPool);

// A full hit with no new input has an unknown arrival rate: unique-token
// throughput cannot be used as its denominator. It must never produce NaN.
const fullHit = S.prefillScenario({
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
assert.ok(Number.isFinite(fullHit.readPerRequest));

// An external request rate is the only defined way to size U=0 load.
const fullHitArrival = S.prefillScenario({
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
  37 * fullHitArrival.readPerRequest,
  0,
  "external lambda at U=0"
);

// With q=0 and no writes, bandwidth is exactly zero even though request rate
// cannot be inferred.
const localFullHit = S.prefillScenario({
  ...baseTraffic,
  contextTokens: 1024,
  hitRate: 1,
  blockTokens: 256,
  remoteFraction: 0,
  readBudgetGBsPool: 1,
});
assert.equal(localFullHit.offeredReqsPool, null);
assert.equal(localFullHit.readPerRequest, 0);
assert.equal(localFullHit.writePerRequest, 0);
assert.equal(localFullHit.requiredRead, 0);
assert.equal(localFullHit.requiredWrite, 0);
assert.equal(localFullHit.readReqCeiling, Infinity);

// Disabling state writes still writes only newly generated growing KV.
const noStateWrite = S.prefillScenario({
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
const fractional = S.prefillScenario({
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

// The Kimi Agentic preset is trace-backed at token/hour granularity. It does
// not invent API req/s or amortize KDA fixed state over unknown requests.
const kimiPreset = S.AGENTIC_PRESETS.kimiK3SingleUserPeak;
assert.equal(kimiPreset.modelId, "k3");
assert.equal(kimiPreset.profileId, "fp8-vllm-state");
assert.equal(kimiPreset.usageEvents, 5);
assert.equal(kimiPreset.promptTokensHour, 21_215_355);
assert.equal(kimiPreset.cacheReadTokensHour, 20_131_868);
close(
  kimiPreset.averageHitRate,
  255_158_901 / (255_158_901 + 22_481_047),
  1e-15,
  "Kimi average token-weighted hit"
);
const agenticKimi = S.prefillScenario({
  ...baseTraffic,
  loadMode: "agentic",
  hitRate: kimiPreset.averageHitRate,
  agenticPromptTokensPerSecond: kimiPreset.promptTokensHour / 3600,
});
close(
  agenticKimi.agenticCacheReadTokensPerSecond,
  (kimiPreset.promptTokensHour / 3600) * kimiPreset.averageHitRate,
  1e-12,
  "Kimi peak prompt with average hit"
);
close(
  agenticKimi.selectedGrowingBytesPerToken,
  24 * 576,
  0,
  "Kimi FP8 growing bytes/token"
);
close(
  agenticKimi.requiredGrowingRead / S.GB,
  0.07487042461886444,
  1e-12,
  "Kimi Agentic growing-KV load GB/s"
);
assert.equal(agenticKimi.offeredReqsPool, null);
assert.equal(agenticKimi.requiredStateRead, null);
assert.equal(agenticKimi.requiredRead, null);
assert.equal(agenticKimi.requiredWrite, null);
const agenticBudget = S.prefillScenario({
  ...baseTraffic,
  loadMode: "agentic",
  hitRate: kimiPreset.averageHitRate,
  agenticPromptTokensPerSecond: kimiPreset.promptTokensHour / 3600,
  readBudgetGBsPool: 1,
});
assert.equal(agenticBudget.readReqCeiling, null);
close(
  agenticBudget.agenticReadBudgetRatio,
  1e9 / agenticBudget.requiredGrowingRead,
  1e-12,
  "Agentic read-budget headroom"
);
const agenticLocal = S.prefillScenario({
  ...baseTraffic,
  loadMode: "agentic",
  hitRate: kimiPreset.averageHitRate,
  agenticPromptTokensPerSecond: kimiPreset.promptTokensHour / 3600,
  remoteFraction: 0,
});
assert.equal(agenticLocal.requiredGrowingRead, 0);
assert.equal(agenticLocal.requiredStateRead, 0);
assert.equal(agenticLocal.requiredRead, 0);
[
  "handoffMode",
  "decodeReadPerRequest",
  "directPerRequest",
  "requiredDirect",
  "pdLinkReqCeiling",
].forEach((field) => assert.ok(!(field in agenticKimi), `${field} removed`));

// Capacity retention starts with one shared cache lineage per user. Prefix-hit
// bytes are anchored once, while only newly-computed growing KV accumulates
// during the 5/30/60-minute windows.
const capacity90 = S.capacityScenario({
  modelId: "k3",
  profileId: "fp8-vllm-state",
  contextTokens: ONE_MI_TOKEN,
  extraTokens: 0,
  blockTokens: 256,
  hitRate: 0.9,
  gpus: 72,
  uniqueTpsGpu: 10_000,
  retainedUsers: 1_000,
  poolCopies: 2,
  usableFraction: 0.8,
});
const capacity95 = S.capacityScenario({
  modelId: "k3",
  profileId: "fp8-vllm-state",
  contextTokens: ONE_MI_TOKEN,
  extraTokens: 0,
  blockTokens: 256,
  hitRate: 0.95,
  gpus: 72,
  uniqueTpsGpu: 10_000,
  retainedUsers: 1_000,
  poolCopies: 2,
  usableFraction: 0.8,
});
assert.equal(capacity90.matchedTokens, 943_616);
assert.equal(capacity95.matchedTokens, 996_096);
assert.equal(capacity90.uniqueTokens, 104_960);
assert.equal(capacity95.uniqueTokens, 52_480);
close(
  capacity90.requestRate,
  720_000 / 104_960,
  1e-12,
  "90% capacity request rate"
);
close(
  capacity95.requestRate,
  720_000 / 52_480,
  1e-12,
  "95% capacity request rate"
);
assert.equal(
  capacity90.sharedGrowingBytesPerUser,
  capacity90.matchedLayout.selectedGrowingBytes
);
assert.equal(
  capacity90.latestStateBytesPerUser,
  capacity90.finalLayout.selectedStateBytes
);
close(
  capacity90.anchorBytesPerUser,
  capacity90.sharedGrowingBytesPerUser +
    capacity90.latestStateBytesPerUser,
  0,
  "one shared growing prefix plus one latest state per user"
);
close(
  capacity90.userAnchorBytes,
  1_000 * capacity90.anchorBytesPerUser,
  0,
  "user-count anchor scaling"
);
close(
  capacity90.userAnchorBytes -
    1_000 * capacity90.sharedGrowingBytesPerUser,
  1_000 * capacity90.latestStateBytesPerUser,
  0,
  "fixed state retained once per user"
);
close(
  capacity90.newGrowingBytesPerRequest,
  capacity90.finalLayout.selectedGrowingBytes -
    capacity90.matchedLayout.selectedGrowingBytes,
  0,
  "only non-hit growing KV is new"
);
close(
  capacity90.newGrowingBytesPerSecond,
  capacity90.requestRate * capacity90.newGrowingBytesPerRequest,
  1e-12,
  "new growing KV rate"
);
close(
  capacity90.newGrowingBytesPerSecond,
  720_000 * 24 * 576,
  1e-12,
  "linear Kimi growing bytes at GPU saturation"
);
close(
  capacity95.newGrowingBytesPerSecond,
  capacity90.newGrowingBytesPerSecond,
  1e-12,
  "linear growing rate independent of hit rate"
);
assert.deepEqual(
  capacity90.windows.map((window) => window.seconds),
  [300, 1800, 3600]
);
close(
  capacity90.windows[1].newGrowingBytes,
  6 * capacity90.windows[0].newGrowingBytes,
  1e-12,
  "30-minute growth is six times 5-minute growth"
);
close(
  capacity90.windows[2].newGrowingBytes,
  12 * capacity90.windows[0].newGrowingBytes,
  1e-12,
  "1-hour growth is twelve times 5-minute growth"
);
capacity90.windows.forEach((window) => {
  close(
    window.logicalBytes,
    capacity90.userAnchorBytes + window.newGrowingBytes,
    0,
    "logical capacity does not duplicate hit bytes"
  );
  close(
    window.rawBytes,
    (window.logicalBytes * 2) / 0.8,
    0,
    "raw conversion applied exactly once"
  );
});

const twiceTheUsers = S.capacityScenario({
  modelId: "k3",
  profileId: "fp8-vllm-state",
  contextTokens: ONE_MI_TOKEN,
  blockTokens: 256,
  hitRate: 0.9,
  gpus: 72,
  uniqueTpsGpu: 10_000,
  retainedUsers: 2_000,
  poolCopies: 2,
  usableFraction: 0.8,
});
close(
  twiceTheUsers.userAnchorBytes,
  2 * capacity90.userAnchorBytes,
  0,
  "doubling users doubles the starting anchor"
);
close(
  twiceTheUsers.windows[0].newGrowingBytes,
  capacity90.windows[0].newGrowingBytes,
  0,
  "fixed GPU fleet keeps aggregate new KV unchanged"
);

const fullHitCapacity = S.capacityScenario({
  modelId: "k3",
  profileId: "fp8-vllm-state",
  contextTokens: ONE_MI_TOKEN,
  blockTokens: 256,
  hitRate: 1,
  gpus: 72,
  uniqueTpsGpu: 10_000,
  retainedUsers: 1_000,
  poolCopies: 1,
  usableFraction: 0.8,
});
assert.equal(fullHitCapacity.uniqueTokens, 0);
assert.equal(fullHitCapacity.requestRate, null);
assert.equal(fullHitCapacity.newGrowingBytesPerSecond, 0);
assert.equal(
  fullHitCapacity.windows[0].logicalBytes,
  fullHitCapacity.userAnchorBytes
);

const unknownCapacityUsable = S.capacityScenario({
  modelId: "k3",
  profileId: "fp8-vllm-state",
  contextTokens: ONE_MI_TOKEN,
  blockTokens: 256,
  hitRate: 0.9,
  gpus: 72,
  uniqueTpsGpu: 10_000,
  retainedUsers: 1_000,
  poolCopies: 1,
  usableFraction: 0,
});
assert.ok(Number.isFinite(unknownCapacityUsable.windows[0].logicalBytes));
assert.equal(unknownCapacityUsable.rawUserAnchorBytes, null);
assert.equal(unknownCapacityUsable.windows[0].rawBytes, null);

// Hit comparison keeps Prefill GPUs saturated. The default Agentic view sizes
// one Kimi hot prefix and leaves unmeasured retained writes disabled.
const comparison = S.hitComparisonScenario({
  modelId: "k3",
  profileId: "fp8-vllm-state",
  contextTokens: ONE_MI_TOKEN,
  extraTokens: 0,
  blockTokens: 256,
  remoteFraction: 1,
  admissionFraction: 1,
  writeState: true,
  gpus: 72,
  uniqueTpsGpu: 10_000,
  hitRateLow: 0.9,
  hitRateHigh: 0.95,
  activeSessions: 1,
  retentionSeconds: 600,
  includeRetainedWrites: false,
  poolCopies: 1,
  usableFraction: 0.8,
});
assert.equal(comparison.low.prefill.matchedTokens, 943_616);
assert.equal(comparison.high.prefill.matchedTokens, 996_096);
assert.equal(comparison.low.prefill.uniqueTokens, 104_960);
assert.equal(comparison.high.prefill.uniqueTokens, 52_480);
close(
  comparison.low.prefill.offeredReqsPool,
  6.859756097560975,
  1e-12,
  "90% compute-constrained request rate"
);
close(
  comparison.high.prefill.offeredReqsPool,
  13.71951219512195,
  1e-12,
  "95% compute-constrained request rate"
);
close(
  comparison.low.rawStorageBytes / S.GiB,
  15.708990097045898,
  1e-12,
  "90% raw storage"
);
close(
  comparison.high.rawStorageBytes / S.GiB,
  16.553564071655273,
  1e-12,
  "95% raw storage"
);
close(
  comparison.storageCostIncrease,
  0.05376373461258965,
  1e-12,
  "storage capacity cost increase"
);
assert.equal(comparison.computeReqIncrease, 1);
close(
  comparison.readIncrease,
  1.1075274692251793,
  1e-12,
  "read bandwidth increase"
);
assert.equal(comparison.writeIncrease, null);
assert.equal(comparison.low.retainedWriteBytes, 0);
assert.equal(comparison.high.retainedWriteBytes, 0);

const withRetainedWrites = S.hitComparisonScenario({
  modelId: "k3",
  profileId: "fp8-vllm-state",
  contextTokens: ONE_MI_TOKEN,
  blockTokens: 256,
  remoteFraction: 1,
  admissionFraction: 1,
  writeState: true,
  gpus: 72,
  uniqueTpsGpu: 10_000,
  hitRateLow: 0.9,
  hitRateHigh: 0.95,
  activeSessions: 1,
  retentionSeconds: 600,
  includeRetainedWrites: true,
  poolCopies: 1,
  usableFraction: 0.8,
});
assert.ok(
  withRetainedWrites.low.rawStorageBytes > comparison.low.rawStorageBytes
);
assert.ok(withRetainedWrites.low.retainedWriteBytes > 0);
assert.ok(withRetainedWrites.writeIncrease != null);

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
assert.equal(S.fmtGBs(0.07487042461886444 * S.GB), "0.07487 GB/s");
assert.equal(S.fmtCapacity(S.TiB), "1.00 TiB");
assert.equal(S.fmtRate(NaN), "N/A");
assert.equal(S.fmtRate(Infinity), "∞");

console.log("ok");
console.log("  V4-Pro growing KV     =", S.fmtGiB(layout("v4pro", "bf16-logical").growingBytes));
console.log("  V4-Pro blog paged KV  =", S.fmtGiB(layout("v4pro", "bf16-logical").pagedKvBytes));
console.log("  V4-Pro + comp. state  =", S.fmtGiB(layout("v4pro", "bf16-logical").totalBytes));
console.log("  GLM-5.2 base FP8      =", S.fmtGiB(layout("glm52", "fp8-logical").totalBytes));
console.log("  GLM-5.3-Flash BF16    =", S.fmtGiB(layout("glm53flash", "bf16-logical").totalBytes));
console.log("  Kimi K3 FP8 + state   =", S.fmtGiB(layout("k3", "fp8-vllm-state").totalBytes));
