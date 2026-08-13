/**
 * CMX analytical demand/capacity model.
 *
 * This is not a benchmark and does not contain a published CMX performance
 * model. It separates:
 *   1. model/layout bytes,
 *   2. compute-offered request load,
 *   3. required CMX traffic,
 *   4. an optional user-supplied transfer ceiling.
 *
 * Browser and Node share this file. Storage uses bytes/GiB; bandwidth uses
 * decimal GB/s.
 */
(function (root) {
  "use strict";

  var KiB = 1024;
  var MiB = 1024 * KiB;
  var GiB = 1024 * MiB;
  var GB = 1e9;
  var SWA_TOKENS = 128;

  var MODELS = {
    v4pro: {
      id: "v4pro",
      name: "DeepSeek V4-Pro",
      kind: "v4",
      nC4: 30,
      nC128: 31,
      nSwaOnly: 0,
      evidence: "vLLM 2026-04-24 appendix + DeepSeek-V4 §4.2.1",
    },
    v4flash: {
      id: "v4flash",
      name: "DeepSeek V4-Flash",
      kind: "v4",
      nC4: 21,
      nC128: 20,
      nSwaOnly: 2,
      evidence: "DeepSeek-V4 §4.2.1; derived layer arithmetic",
    },
    glm52: {
      id: "glm52",
      name: "GLM-5.2",
      kind: "glm",
      nLayers: 78,
      nIndexer: 21,
      evidence: "GLM-5.2 config/blog; MTP is an explicit optional layer",
    },
    k3: {
      id: "k3",
      name: "Kimi K3",
      kind: "k3",
      nMla: 24,
      nKda: 69,
      kdaHeads: 96,
      kdaDim: 128,
      convKernel: 4,
      evidence: "moonshotai/Kimi-K3 config + vLLM KDA state implementation",
    },
  };

  var PROFILES = {
    v4pro: [
      {
        id: "bf16-logical",
        label: "BF16 公式布局（1024 B main / 256 B index）",
        mainEntryBytes: 1024,
        indexEntryBytes: 256,
        confidence: "published",
        note: "BF16 paged-KV 公式布局；可继续会话还需 vLLM FP32 compressor state。",
      },
      {
        id: "fp8-vllm",
        label: "vLLM fp8_ds_mla + FP8 index（584 / 132 B）",
        mainEntryBytes: 584,
        indexEntryBytes: 132,
        confidence: "implementation",
        note: "引擎页布局，另计 FP32 compressor state；不等于 CMX 序列化格式。",
      },
      {
        id: "fp8-fp4-payload",
        label: "FP8 main + FP4 index payload（584 / 68 B）",
        mainEntryBytes: 584,
        indexEntryBytes: 68,
        confidence: "estimate",
        note: "payload 估算，另计 FP32 compressor state；实际页分配可更大。",
      },
    ],
    v4flash: null,
    glm52: [
      {
        id: "bf16-logical",
        label: "BF16 逻辑布局（1152 B MLA / 256 B index）",
        mainEntryBytes: 1152,
        indexEntryBytes: 256,
        confidence: "derived",
        note: "按 576 个 BF16 元素；不含页对齐。",
      },
      {
        id: "fp8-logical",
        label: "FP8 逻辑 MLA + 132 B index",
        mainEntryBytes: 576,
        indexEntryBytes: 132,
        confidence: "implementation",
        note: "MLA 是逻辑 576 B；index entry 按实现 132 B。",
      },
      {
        id: "fp8-ds-mla",
        label: "vLLM fp8_ds_mla（656 B MLA / 132 B index）",
        mainEntryBytes: 656,
        indexEntryBytes: 132,
        confidence: "implementation",
        note: "vLLM MLAAttentionSpec 的 656 B 引擎布局。",
      },
    ],
    k3: [
      {
        id: "bf16-vllm-state",
        label: "BF16 MLA + vLLM KDA state",
        mainEntryBytes: 1152,
        confidence: "implementation",
        note: "KDA conv 跟模型 dtype；recurrent 固定 FP32。",
      },
      {
        id: "fp8-vllm-state",
        label: "FP8 MLA + vLLM 默认 KDA state",
        mainEntryBytes: 576,
        confidence: "mixed",
        note: "MLA 按 FP8；KDA conv 默认仍按 BF16，recurrent 为 FP32。",
      },
    ],
  };
  PROFILES.v4flash = PROFILES.v4pro;

  function numberOr(value, fallback) {
    var n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, numberOr(value, min)));
  }

  function floorNearInteger(value) {
    var nearest = Math.round(value);
    var tolerance =
      Number.EPSILON * Math.max(1, Math.abs(value)) * 4;
    return Math.abs(value - nearest) <= tolerance
      ? nearest
      : Math.floor(value);
  }

  function profileFor(modelId, profileId) {
    var profiles = PROFILES[modelId];
    if (!profiles) throw new Error("unknown model: " + modelId);
    return profiles.find(function (p) {
      return p.id === profileId;
    }) || profiles[0];
  }

  function profileOptions(modelId) {
    return (PROFILES[modelId] || []).slice();
  }

  function kdaStateBytes(model, profile) {
    var recurrentPerLayer =
      model.kdaHeads * model.kdaDim * model.kdaDim * 4; // FP32 in vLLM
    var convElemsPerLayer =
      (model.kdaHeads * model.kdaDim * 3) * (model.convKernel - 1);
    // vLLM's default KDA conv state follows the BF16 model/cache dtype even
    // when the growing MLA cache is FP8; recurrent state is always FP32.
    var convBytesPerElem = 2;
    return model.nKda * (recurrentPerLayer + convElemsPerLayer * convBytesPerElem);
  }

  function v4CompressorStateBytes(model) {
    // vLLM CompressorStateCache is FP32. For C4, both the main compressor
    // (head=512) and indexer compressor (head=128) retain 8 rows. For C128,
    // the main compressor retains 128 rows. These residuals are required to
    // continue compression after a P/D or storage handoff.
    var c4MainPerLayer = 8 * (2 * 2 * 512) * 4;
    var c4IndexerPerLayer = 8 * (2 * 2 * 128) * 4;
    var c128MainPerLayer = 128 * (2 * 1 * 512) * 4;
    return (
      model.nC4 * (c4MainPerLayer + c4IndexerPerLayer) +
      model.nC128 * c128MainPerLayer
    );
  }

  function sessionLayout(p) {
    var model = MODELS[p.modelId];
    if (!model) throw new Error("unknown model: " + p.modelId);
    var profile = profileFor(p.modelId, p.profileId);
    var tokens = Math.max(0, Math.floor(numberOr(p.tokens, 0)));
    var includeMtp = !!p.includeMtp && model.kind === "glm";
    var growingBytes = 0;
    var stateBytes = 0;
    var swaStateBytes = 0;
    var compressorStateBytes = 0;
    var growingBytesPerToken = 0;

    if (model.kind === "v4") {
      // A compressed row is materialized only when the source group closes.
      // Incomplete groups live in the compressor residual state below.
      var c4Entries = Math.floor(tokens / 4);
      var c128Entries = Math.floor(tokens / 128);
      growingBytes =
        model.nC4 * c4Entries * (profile.mainEntryBytes + profile.indexEntryBytes) +
        model.nC128 * c128Entries * profile.mainEntryBytes;
      swaStateBytes =
        (model.nC4 + model.nC128 + model.nSwaOnly) *
        SWA_TOKENS *
        profile.mainEntryBytes;
      compressorStateBytes = v4CompressorStateBytes(model);
      stateBytes = swaStateBytes + compressorStateBytes;
      growingBytesPerToken =
        model.nC4 * (profile.mainEntryBytes + profile.indexEntryBytes) / 4 +
        model.nC128 * profile.mainEntryBytes / 128;
    } else if (model.kind === "glm") {
      var nLayers = model.nLayers + (includeMtp ? 1 : 0);
      var nIndexer = model.nIndexer + (includeMtp ? 1 : 0);
      growingBytesPerToken =
        nLayers * profile.mainEntryBytes + nIndexer * profile.indexEntryBytes;
      growingBytes = tokens * growingBytesPerToken;
    } else {
      growingBytesPerToken = model.nMla * profile.mainEntryBytes;
      growingBytes = tokens * growingBytesPerToken;
      stateBytes = kdaStateBytes(model, profile);
    }

    return {
      model: model,
      profile: profile,
      tokens: tokens,
      includeMtp: includeMtp,
      growingBytes: growingBytes,
      stateBytes: stateBytes,
      swaStateBytes: swaStateBytes,
      compressorStateBytes: compressorStateBytes,
      pagedKvBytes: growingBytes + swaStateBytes,
      totalBytes: growingBytes + stateBytes,
      growingBytesPerToken: growingBytesPerToken,
    };
  }

  function matchedTokens(contextTokens, hitRate, blockTokens) {
    var context = Math.max(0, Math.floor(numberOr(contextTokens, 0)));
    var hit = clamp(hitRate, 0, 1);
    var block = Math.max(1, Math.floor(numberOr(blockTokens, 1)));
    var matchedBlocks = floorNearInteger((context * hit) / block);
    return Math.min(context, matchedBlocks * block);
  }

  function bytesAtRate(requestRate, bytesPerRequest) {
    if (bytesPerRequest === 0) return 0;
    return requestRate == null ? null : requestRate * bytesPerRequest;
  }

  function trafficScenario(p) {
    var context = Math.max(0, Math.floor(numberOr(p.contextTokens, 0)));
    var extra = Math.max(0, Math.floor(numberOr(p.extraTokens, 0)));
    var matched = matchedTokens(context, p.hitRate, p.blockTokens);
    var uniqueTokens = context - matched + extra;
    var finalTokens = context + extra;
    var remoteFraction = clamp(p.remoteFraction, 0, 1);
    var admissionFraction = clamp(p.admissionFraction, 0, 1);
    var writeState = p.writeState !== false;
    var gpus = Math.max(1, Math.floor(numberOr(p.gpus, 1)));
    var uniqueTpsGpu = Math.max(0, numberOr(p.uniqueTpsGpu, 0));

    var common = {
      modelId: p.modelId,
      profileId: p.profileId,
      includeMtp: !!p.includeMtp,
    };
    var matchedLayout = sessionLayout(Object.assign({}, common, { tokens: matched }));
    var finalLayout = sessionLayout(Object.assign({}, common, { tokens: finalTokens }));

    var readPerRequest =
      matched > 0 ? remoteFraction * matchedLayout.totalBytes : 0;
    var growingDelta = Math.max(
      0,
      finalLayout.growingBytes - matchedLayout.growingBytes
    );
    var stateWrite =
      writeState && uniqueTokens > 0 ? finalLayout.stateBytes : 0;
    var writePerRequest =
      admissionFraction * (growingDelta + stateWrite);

    // U=0 means this input cannot derive an arrival rate: the request is not
    // bounded by unique-token compute. Represent that as unknown, not infinity.
    var offeredReqsGpu =
      uniqueTokens > 0 ? uniqueTpsGpu / uniqueTokens : null;
    var offeredReqsPool =
      offeredReqsGpu == null ? null : offeredReqsGpu * gpus;
    var requiredRead = bytesAtRate(offeredReqsPool, readPerRequest);
    var requiredWrite = bytesAtRate(offeredReqsPool, writePerRequest);

    var linkGBsGpu = Math.max(0, numberOr(p.linkGBsGpu, 0));
    var linkBudgetPool = linkGBsGpu * GB * gpus;
    var linkMode = p.linkMode === "full-duplex" ? "full-duplex" : "shared";
    var linkReqCeiling = null;
    if (linkBudgetPool > 0) {
      if (linkMode === "full-duplex") {
        var readCeiling =
          readPerRequest > 0 ? linkBudgetPool / readPerRequest : Infinity;
        var writeCeiling =
          writePerRequest > 0 ? linkBudgetPool / writePerRequest : Infinity;
        linkReqCeiling = Math.min(readCeiling, writeCeiling);
      } else {
        var bytesPerRequest = readPerRequest + writePerRequest;
        linkReqCeiling =
          bytesPerRequest > 0 ? linkBudgetPool / bytesPerRequest : Infinity;
      }
    }
    var cappedReqsPool =
      linkReqCeiling == null || offeredReqsPool == null
        ? null
        : Math.min(offeredReqsPool, linkReqCeiling);

    return {
      model: finalLayout.model,
      profile: finalLayout.profile,
      contextTokens: context,
      extraTokens: extra,
      finalTokens: finalTokens,
      matchedTokens: matched,
      uniqueTokens: uniqueTokens,
      hitRate: clamp(p.hitRate, 0, 1),
      blockTokens: Math.max(1, Math.floor(numberOr(p.blockTokens, 1))),
      remoteFraction: remoteFraction,
      admissionFraction: admissionFraction,
      writeState: writeState,
      gpus: gpus,
      uniqueTpsGpu: uniqueTpsGpu,
      matchedLayout: matchedLayout,
      finalLayout: finalLayout,
      readPerRequest: readPerRequest,
      writePerRequest: writePerRequest,
      stateWriteBytes: admissionFraction * stateWrite,
      offeredReqsGpu: offeredReqsGpu,
      offeredReqsPool: offeredReqsPool,
      requiredRead: requiredRead,
      requiredWrite: requiredWrite,
      linkGBsGpu: linkGBsGpu,
      linkBudgetPool: linkBudgetPool,
      linkMode: linkMode,
      linkReqCeiling: linkReqCeiling,
      cappedReqsPool: cappedReqsPool,
      cappedRead: bytesAtRate(cappedReqsPool, readPerRequest),
      cappedWrite: bytesAtRate(cappedReqsPool, writePerRequest),
    };
  }

  function capacityScenario(p) {
    var layout = sessionLayout({
      modelId: p.modelId,
      profileId: p.profileId,
      tokens: p.tokens,
      includeMtp: p.includeMtp,
    });
    var bytes = layout.totalBytes;
    var usableHbmGB = Math.max(0, numberOr(p.usableHbmGB, 0));
    var usableG2TB = Math.max(0, numberOr(p.usableG2TB, 0));
    var rawCmxPB = Math.max(0, numberOr(p.rawCmxPB, 0));
    var cmxUsableFraction = clamp(p.cmxUsableFraction, 0, 1);
    var hbmBytes = usableHbmGB * GB;
    var g2Bytes = usableG2TB * 1e12;
    var rawCmxBytes = rawCmxPB * 1e15;
    var usableCmxBytes = rawCmxBytes * cmxUsableFraction;
    return {
      layout: layout,
      usableHbmGB: usableHbmGB,
      usableG2TB: usableG2TB,
      rawCmxPB: rawCmxPB,
      cmxUsableFraction: cmxUsableFraction,
      hbmSessionsPerGpu: bytes > 0 ? Math.floor(hbmBytes / bytes) : null,
      g2SessionsPerRack: bytes > 0 ? Math.floor(g2Bytes / bytes) : null,
      rawCmxSessionsPerPod: bytes > 0 ? Math.floor(rawCmxBytes / bytes) : null,
      usableCmxSessionsPerPod:
        bytes > 0 ? Math.floor(usableCmxBytes / bytes) : null,
    };
  }

  function fmtGiB(bytes) {
    if (bytes == null) return "N/A";
    if (Number.isNaN(bytes)) return "N/A";
    if (!Number.isFinite(bytes)) return "∞";
    var gib = bytes / GiB;
    if (gib >= 10) return gib.toFixed(2) + " GiB";
    if (gib >= 1) return gib.toFixed(3) + " GiB";
    if (gib >= 0.01) return (gib * 1024).toFixed(2) + " MiB";
    return (bytes / KiB).toFixed(1) + " KiB";
  }

  function fmtGBs(bytesPerSecond) {
    if (bytesPerSecond == null) return "N/A";
    if (Number.isNaN(bytesPerSecond)) return "N/A";
    if (!Number.isFinite(bytesPerSecond)) return "∞";
    var value = bytesPerSecond / GB;
    if (Math.abs(value) >= 100) return value.toFixed(0) + " GB/s";
    if (Math.abs(value) >= 10) return value.toFixed(1) + " GB/s";
    return value.toFixed(2) + " GB/s";
  }

  function fmtRate(value) {
    if (value == null) return "N/A";
    if (Number.isNaN(value)) return "N/A";
    if (!Number.isFinite(value)) return "∞";
    if (value >= 1e6) return (value / 1e6).toFixed(2) + " M";
    if (value >= 1e3) return (value / 1e3).toFixed(2) + " k";
    return value.toFixed(2);
  }

  var api = {
    KiB: KiB,
    MiB: MiB,
    GiB: GiB,
    GB: GB,
    MODELS: MODELS,
    PROFILES: PROFILES,
    profileFor: profileFor,
    profileOptions: profileOptions,
    sessionLayout: sessionLayout,
    matchedTokens: matchedTokens,
    trafficScenario: trafficScenario,
    capacityScenario: capacityScenario,
    fmtGiB: fmtGiB,
    fmtGBs: fmtGBs,
    fmtRate: fmtRate,
  };

  root.CmxSim = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
