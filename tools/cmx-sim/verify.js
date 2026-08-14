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

// Published/derived paged-KV anchors, plus continuation state required by
// the implementation.
close(
  layout("v4pro", "bf16-logical").pagedKvBytes / S.GiB,
  9.6246337890625,
  1e-12,
  "V4-Pro BF16 paged KV"
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

// V4 emits compressed rows only at complete C4/C128 group boundaries.
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

// Cache economics keeps token hit, prefill compute share, and cache cost on
// separate denominators. A 20% compute saving is not implied by hit rate alone.
const economics = S.economicsScenario({
  hitRate: 0.9229385582485566,
  prefillComputeShare: 0.22,
  avoidEfficiency: 1,
  cacheCostShare: 0.05,
  targetComputeSavings: 0.2,
});
close(
  economics.grossComputeSavings,
  0.22 * 0.9229385582485566,
  1e-15,
  "gross compute savings"
);
close(
  economics.netSavings,
  economics.grossComputeSavings - 0.05,
  1e-15,
  "net savings"
);
close(
  economics.requiredPrefillComputeShare,
  0.2 / 0.9229385582485566,
  1e-15,
  "required prefill share"
);
close(
  economics.grossBenefitCostRatio,
  economics.grossComputeSavings / 0.05,
  1e-15,
  "gross benefit/cost"
);
close(
  economics.netRoi,
  economics.netSavings / 0.05,
  1e-15,
  "net ROI"
);

const zeroEconomics = S.economicsScenario({
  hitRate: 0,
  prefillComputeShare: 2,
  avoidEfficiency: 1,
  cacheCostShare: 0,
  targetComputeSavings: 0.2,
});
assert.equal(zeroEconomics.prefillComputeShare, 1);
assert.equal(zeroEconomics.grossComputeSavings, 0);
assert.equal(zeroEconomics.requiredPrefillComputeShare, null);
assert.equal(zeroEconomics.grossBenefitCostRatio, null);
assert.equal(zeroEconomics.netRoi, null);

// Formatter edge cases distinguish unknown/invalid from an unbounded ceiling.
assert.equal(S.fmtGiB(null), "N/A");
assert.equal(S.fmtGiB(NaN), "N/A");
assert.equal(S.fmtGBs(NaN), "N/A");
assert.equal(S.fmtRate(NaN), "N/A");
assert.equal(S.fmtRate(Infinity), "∞");

console.log("ok");
console.log("  V4-Pro BF16 paged KV  =", S.fmtGiB(layout("v4pro", "bf16-logical").pagedKvBytes));
console.log("  V4-Pro + comp. state  =", S.fmtGiB(layout("v4pro", "bf16-logical").totalBytes));
console.log("  GLM-5.2 base FP8      =", S.fmtGiB(layout("glm52", "fp8-logical").totalBytes));
console.log("  Kimi K3 FP8 + state   =", S.fmtGiB(layout("k3", "fp8-vllm-state").totalBytes));
