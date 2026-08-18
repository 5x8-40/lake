/**
 * CMX analytical demand/capacity model.
 *
 * This is not a benchmark and does not contain a published CMX performance
 * model. It separates:
 *   1. model/layout bytes,
 *   2. Agentic token demand or compute-constrained request throughput,
 *   3. Prefill-side KV load bandwidth,
 *   4. an optional user-supplied storage-read ceiling,
 *   5. a GPU-saturated hit-rate capacity comparison.
 *
 * Browser and Node share this file. Storage uses bytes/GiB; bandwidth uses
 * decimal GB/s.
 */
(function (root) {
  "use strict";

  var KiB = 1024;
  var MiB = 1024 * KiB;
  var GiB = 1024 * MiB;
  var TiB = 1024 * GiB;
  var PiB = 1024 * TiB;
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
      hasMtp: true,
      evidence: "vLLM 2026-04-24 appendix + DeepSeek-V4 §4.2.1",
    },
    v4flash: {
      id: "v4flash",
      name: "DeepSeek V4-Flash",
      kind: "v4",
      nC4: 21,
      nC128: 20,
      nSwaOnly: 2,
      hasMtp: true,
      evidence: "DeepSeek-V4 §4.2.1; derived layer arithmetic",
    },
    glm52: {
      id: "glm52",
      name: "GLM-5.2",
      kind: "glm",
      nLayers: 78,
      nIndexer: 21,
      hasMtp: true,
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
        label: "BF16 logical（1024 B main / 256 B index）",
        mainEntryBytes: 1024,
        indexEntryBytes: 256,
        byteClass: "logical-payload",
        confidence: "published",
        note: "BF16 growing + SWA；续算需额外计入 FP32 compressor state。",
      },
      {
        id: "fp8-vllm",
        label: "vLLM FP8 entry payload（584 / 132 B）",
        mainEntryBytes: 584,
        indexEntryBytes: 132,
        byteClass: "engine-entry-payload",
        confidence: "implementation",
        note: "Entry payload；pages 另计；不代表 CMX wire。",
      },
      {
        id: "fp8-fp4-payload",
        label: "FP8 main + FP4 index payload（584 / 68 B）",
        mainEntryBytes: 584,
        indexEntryBytes: 68,
        byteClass: "payload-estimate",
        confidence: "estimate",
        note: "Payload estimate；vLLM pages 仍按 132 B index 分配。",
      },
    ],
    v4flash: null,
    glm52: [
      {
        id: "bf16-logical",
        label: "BF16 逻辑布局（1152 B MLA / 256 B index）",
        mainEntryBytes: 1152,
        indexEntryBytes: 256,
        byteClass: "logical-payload",
        confidence: "derived",
        note: "按 576 个 BF16 元素；不含页对齐。",
      },
      {
        id: "fp8-logical",
        label: "FP8 逻辑 MLA + 132 B index",
        mainEntryBytes: 576,
        indexEntryBytes: 132,
        byteClass: "logical-payload",
        confidence: "implementation",
        note: "MLA 是逻辑 576 B；index entry 按实现 132 B。",
      },
      {
        id: "fp8-ds-mla",
        label: "vLLM FP8 entry payload（656 B MLA / 132 B index）",
        mainEntryBytes: 656,
        indexEntryBytes: 132,
        byteClass: "engine-entry-payload",
        confidence: "implementation",
        note: "vLLM entry payload；未计 page alignment 和 allocator reserve。",
      },
    ],
    k3: [
      {
        id: "bf16-vllm-state",
        label: "BF16 MLA + vLLM KDA state",
        mainEntryBytes: 1152,
        byteClass: "engine-entry-payload",
        confidence: "implementation",
        note: "KDA conv 跟模型 dtype；recurrent 固定 FP32。",
      },
      {
        id: "fp8-vllm-state",
        label: "FP8 MLA + vLLM 默认 KDA state",
        mainEntryBytes: 576,
        byteClass: "mixed-payload",
        confidence: "mixed",
        note: "MLA 按 FP8；KDA conv 默认仍按 BF16，recurrent 为 FP32。",
      },
    ],
  };
  PROFILES.v4flash = PROFILES.v4pro;

  var AGENTIC_PRESETS = {
    kimiK3SingleUserPeak: {
      id: "kimi-k3-single-user-peak",
      name: "Kimi K3 单用户峰值小时",
      modelId: "k3",
      profileId: "fp8-vllm-state",
      averageHitRate: 0.9190280535566157,
      peakHourHitRate: 0.9489291128995956,
      peakHour: "2026-08-05 15:00 UTC+8",
      usageEvents: 5,
      cacheWriteTokensHour: 0,
      uncachedInputTokensHour: 1083487,
      cacheReadTokensHour: 20131868,
      outputTokensHour: 116710,
      promptTokensHour: 21215355,
      totalTokensHour: 21332065,
      evidence:
        "匿名 Cursor usage trace；event 是聚合账单行，不是模型 API request。",
    },
  };

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

  function roundUp(value, alignment) {
    if (alignment <= 0) return value;
    return Math.ceil(value / alignment) * alignment;
  }

  function ceilDiv(value, divisor) {
    return Math.ceil(value / divisor);
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

  function kdaStateBytes(model) {
    var recurrentPerLayer =
      model.kdaHeads * model.kdaDim * model.kdaDim * 4; // FP32 in vLLM
    var convElemsPerLayer =
      (model.kdaHeads * model.kdaDim * 3) * (model.convKernel - 1);
    // vLLM's default KDA conv state follows the BF16 model/cache dtype even
    // when the growing MLA cache is FP8; recurrent state is always FP32.
    var convBytesPerElem = 2;
    return model.nKda * (recurrentPerLayer + convElemsPerLayer * convBytesPerElem);
  }

  function v4CompressorStateBytes(model, policy) {
    // Base vLLM/SGLang retains 8 C4 rows and 128 C128 rows. SGLang's
    // speculative policy doubles those rings. Online C128 instead keeps one
    // (max, sum, kv) FP32 row and is not the same layout as the raw-token ring.
    var speculative = policy === "speculative";
    var onlineC128 = policy === "online-c128";
    var c4Rows = speculative ? 16 : 8;
    var c128Rows = speculative ? 256 : onlineC128 ? 1 : 128;
    var c4MainPerLayer = c4Rows * (2 * 2 * 512) * 4;
    var c4IndexerPerLayer = c4Rows * (2 * 2 * 128) * 4;
    var c128StateDim = onlineC128 ? 3 * 512 : 2 * 1 * 512;
    var c128MainPerLayer = c128Rows * c128StateDim * 4;
    return (
      model.nC4 * (c4MainPerLayer + c4IndexerPerLayer) +
      model.nC128 * c128MainPerLayer
    );
  }

  function slidingAdmissionPages(tokens, windowTokens, blockTokens, inFlightTokens) {
    if (tokens <= 0) return 0;
    var heldTokens = Math.min(
      tokens,
      Math.max(0, windowTokens - 1 + inFlightTokens)
    );
    // Mirrors SlidingWindowSpec.max_admission_blocks_per_request: an extra
    // block is required because the window can begin mid-block.
    return ceilDiv(heldTokens, blockTokens) + 1;
  }

  function v4EngineAllocation(model, profile, tokens, includeMtp, policy, inFlight) {
    // Only the audited vLLM base-ring layouts have a reproducible page model.
    // Speculative/online policies are SGLang-specific representations.
    if (policy !== "base") return null;

    var isBf16 = profile.id === "bf16-logical";
    var isFp8 =
      profile.id === "fp8-vllm" || profile.id === "fp8-fp4-payload";
    if (!isBf16 && !isFp8) return null;

    var mainEntryBytes = isBf16 ? 1024 : 584;
    // vLLM allocates the FP4 index cache at the FP8 132-byte entry size and
    // uses only the first half of the data region.
    var indexEntryBytes = isBf16 ? 256 : 132;
    var alignment = isBf16 ? 512 : 576;
    var sourceBlocks = tokens > 0 ? ceilDiv(tokens, 256) : 0;
    var c4MainPage = roundUp(64 * mainEntryBytes, alignment);
    var c4IndexPage = roundUp(64 * indexEntryBytes, alignment);
    var c128MainPage = roundUp(2 * mainEntryBytes, alignment);
    var growing =
      sourceBlocks *
      (model.nC4 * (c4MainPage + c4IndexPage) +
        model.nC128 * c128MainPage);

    var swaPage = roundUp(64 * mainEntryBytes, alignment);
    var swaLayers =
      model.nC4 + model.nC128 + model.nSwaOnly + (includeMtp ? 1 : 0);
    var swa =
      swaLayers *
      slidingAdmissionPages(tokens, SWA_TOKENS, 64, inFlight) *
      swaPage;

    var c4MainStatePage = roundUp(4 * (2 * 2 * 512) * 4, alignment);
    var c4IndexerStatePage = roundUp(4 * (2 * 2 * 128) * 4, alignment);
    var c128MainStatePage = roundUp(8 * (2 * 1 * 512) * 4, alignment);
    var compressor =
      model.nC4 *
        slidingAdmissionPages(tokens, 8, 4, inFlight) *
        (c4MainStatePage + c4IndexerStatePage) +
      model.nC128 *
        slidingAdmissionPages(tokens, 128, 8, inFlight) *
        c128MainStatePage;

    return {
      growingBytes: growing,
      swaStateBytes: swa,
      compressorStateBytes: compressor,
      stateBytes: swa + compressor,
      pagedKvBytes: growing + swa,
      totalBytes: growing + swa + compressor,
      note:
        "vLLM page/alignment 与 sliding-window admission estimate；不含全局碎片和 packed-pool sharing。",
    };
  }

  function sessionLayout(p) {
    var model = MODELS[p.modelId];
    if (!model) throw new Error("unknown model: " + p.modelId);
    var profile = profileFor(p.modelId, p.profileId);
    var tokens = Math.max(0, Math.floor(numberOr(p.tokens, 0)));
    var includeMtp = !!p.includeMtp && !!model.hasMtp;
    var compressorPolicy =
      p.compressorPolicy === "speculative" ||
      p.compressorPolicy === "online-c128"
        ? p.compressorPolicy
        : "base";
    var byteMode =
      p.byteMode === "engine-pages" || p.byteMode === "custom-wire"
        ? p.byteMode
        : "payload";
    var customWireFactor = Math.max(0, numberOr(p.customWireFactor, 1));
    var inFlightTokens = Math.max(
      0,
      Math.floor(numberOr(p.inFlightTokens, 0))
    );
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
        (model.nC4 +
          model.nC128 +
          model.nSwaOnly +
          (includeMtp ? 1 : 0)) *
        Math.min(tokens, SWA_TOKENS) *
        profile.mainEntryBytes;
      compressorStateBytes =
        tokens > 0 ? v4CompressorStateBytes(model, compressorPolicy) : 0;
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
      stateBytes = tokens > 0 ? kdaStateBytes(model) : 0;
    }

    var engineAllocation =
      model.kind === "v4"
        ? v4EngineAllocation(
            model,
            profile,
            tokens,
            includeMtp,
            compressorPolicy,
            inFlightTokens
          )
        : null;
    var selectedGrowingBytes = growingBytes;
    var selectedStateBytes = stateBytes;
    var selectedPagedKvBytes = growingBytes + swaStateBytes;
    var selectedTotalBytes = growingBytes + stateBytes;
    var selectedBasis = profile.byteClass;
    if (byteMode === "engine-pages") {
      selectedGrowingBytes = engineAllocation
        ? engineAllocation.growingBytes
        : null;
      selectedStateBytes = engineAllocation
        ? engineAllocation.stateBytes
        : null;
      selectedPagedKvBytes = engineAllocation
        ? engineAllocation.pagedKvBytes
        : null;
      selectedTotalBytes = engineAllocation
        ? engineAllocation.totalBytes
        : null;
      selectedBasis = "engine-page-allocation";
    } else if (byteMode === "custom-wire") {
      selectedGrowingBytes *= customWireFactor;
      selectedStateBytes *= customWireFactor;
      selectedPagedKvBytes *= customWireFactor;
      selectedTotalBytes *= customWireFactor;
      selectedBasis = "custom-wire";
    }

    return {
      model: model,
      profile: profile,
      tokens: tokens,
      includeMtp: includeMtp,
      compressorPolicy: compressorPolicy,
      byteMode: byteMode,
      customWireFactor: customWireFactor,
      inFlightTokens: inFlightTokens,
      growingBytes: growingBytes,
      stateBytes: stateBytes,
      swaStateBytes: swaStateBytes,
      compressorStateBytes: compressorStateBytes,
      pagedKvBytes: growingBytes + swaStateBytes,
      totalBytes: growingBytes + stateBytes,
      growingBytesPerToken: growingBytesPerToken,
      engineAllocation: engineAllocation,
      selectedBasis: selectedBasis,
      selectedGrowingBytes: selectedGrowingBytes,
      selectedStateBytes: selectedStateBytes,
      selectedPagedKvBytes: selectedPagedKvBytes,
      selectedTotalBytes: selectedTotalBytes,
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
    if (bytesPerRequest == null) return null;
    if (bytesPerRequest === 0) return 0;
    return requestRate == null ? null : requestRate * bytesPerRequest;
  }

  function addBytes(left, right) {
    return left == null || right == null ? null : left + right;
  }

  function prefillScenario(p) {
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
      compressorPolicy: p.compressorPolicy,
      byteMode: p.byteMode,
      customWireFactor: p.customWireFactor,
      inFlightTokens: p.inFlightTokens,
    };
    var matchedLayout = sessionLayout(Object.assign({}, common, { tokens: matched }));
    var finalLayout = sessionLayout(Object.assign({}, common, { tokens: finalTokens }));

    var growingReadPerRequest =
      matched > 0 && matchedLayout.selectedTotalBytes != null
        ? remoteFraction * matchedLayout.selectedGrowingBytes
        : matched > 0
          ? null
          : 0;
    var stateReadPerRequest =
      matched > 0 && matchedLayout.selectedStateBytes != null
        ? remoteFraction * matchedLayout.selectedStateBytes
        : matched > 0
          ? null
          : 0;
    var readPerRequest = addBytes(
      growingReadPerRequest,
      stateReadPerRequest
    );
    var growingDelta =
      finalLayout.selectedGrowingBytes == null ||
      matchedLayout.selectedGrowingBytes == null
        ? null
        : Math.max(
            0,
            finalLayout.selectedGrowingBytes -
              matchedLayout.selectedGrowingBytes
          );
    var stateWrite =
      writeState && uniqueTokens > 0
        ? finalLayout.selectedStateBytes
        : 0;
    var writePerRequest =
      growingDelta == null || stateWrite == null
        ? null
        : admissionFraction * (growingDelta + stateWrite);

    var loadMode =
      p.loadMode === "arrival" || p.loadMode === "agentic"
        ? p.loadMode
        : "compute";
    var arrivalReqsPool = Math.max(0, numberOr(p.arrivalReqsPool, 0));
    var agenticPromptTokensPerSecond = Math.max(
      0,
      numberOr(p.agenticPromptTokensPerSecond, 0)
    );
    var agenticCacheReadTokensPerSecond =
      agenticPromptTokensPerSecond * clamp(p.hitRate, 0, 1);
    var selectedGrowingBytesPerToken =
      finalTokens > 0 && finalLayout.selectedGrowingBytes != null
        ? finalLayout.selectedGrowingBytes / finalTokens
        : finalTokens > 0
          ? null
          : 0;
    // Cursor usage events do not expose API request counts. Agentic mode
    // therefore computes token-proportional growing-KV demand but no req/s or
    // fixed-state load rate.
    var offeredReqsPool =
      loadMode === "agentic"
        ? null
        : loadMode === "arrival"
        ? arrivalReqsPool
        : uniqueTokens > 0
          ? (uniqueTpsGpu * gpus) / uniqueTokens
          : null;
    var offeredReqsGpu =
      offeredReqsPool == null ? null : offeredReqsPool / gpus;
    var requiredGrowingRead =
      loadMode === "agentic"
        ? selectedGrowingBytesPerToken == null
          ? null
          : agenticCacheReadTokensPerSecond *
            selectedGrowingBytesPerToken *
            remoteFraction
        : bytesAtRate(offeredReqsPool, growingReadPerRequest);
    var requiredStateRead =
      loadMode === "agentic"
        ? stateReadPerRequest === 0
          ? 0
          : null
        : bytesAtRate(offeredReqsPool, stateReadPerRequest);
    var requiredRead = addBytes(requiredGrowingRead, requiredStateRead);
    var requiredWrite =
      loadMode === "agentic"
        ? null
        : bytesAtRate(offeredReqsPool, writePerRequest);

    var readBudgetGBsPool = Math.max(
      0,
      numberOr(p.readBudgetGBsPool, 0)
    );
    var readBudgetPool = readBudgetGBsPool * GB;
    var readReqCeiling =
      loadMode !== "agentic" && readBudgetPool > 0
        ? readPerRequest == null
          ? null
          : readPerRequest > 0
            ? readBudgetPool / readPerRequest
            : Infinity
        : null;
    var cappedReqsPool =
      readReqCeiling == null || offeredReqsPool == null
        ? null
        : Math.min(offeredReqsPool, readReqCeiling);
    var agenticReadBudgetRatio =
      loadMode === "agentic" &&
      readBudgetPool > 0 &&
      requiredGrowingRead != null &&
      requiredGrowingRead > 0
        ? readBudgetPool / requiredGrowingRead
        : null;

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
      loadMode: loadMode,
      arrivalReqsPool: arrivalReqsPool,
      agenticPromptTokensPerSecond: agenticPromptTokensPerSecond,
      agenticCacheReadTokensPerSecond: agenticCacheReadTokensPerSecond,
      gpus: gpus,
      uniqueTpsGpu: uniqueTpsGpu,
      matchedLayout: matchedLayout,
      finalLayout: finalLayout,
      selectedGrowingBytesPerToken: selectedGrowingBytesPerToken,
      growingReadPerRequest: growingReadPerRequest,
      stateReadPerRequest: stateReadPerRequest,
      readPerRequest: readPerRequest,
      writePerRequest: writePerRequest,
      stateWriteBytes:
        stateWrite == null ? null : admissionFraction * stateWrite,
      offeredReqsGpu: offeredReqsGpu,
      offeredReqsPool: offeredReqsPool,
      requiredGrowingRead: requiredGrowingRead,
      requiredStateRead: requiredStateRead,
      requiredRead: requiredRead,
      requiredWrite: requiredWrite,
      readBudgetGBsPool: readBudgetGBsPool,
      readBudgetPool: readBudgetPool,
      readReqCeiling: readReqCeiling,
      agenticReadBudgetRatio: agenticReadBudgetRatio,
      cappedReqsPool: cappedReqsPool,
      cappedRead: bytesAtRate(cappedReqsPool, readPerRequest),
    };
  }

  function capacityScenario(p) {
    var layout = sessionLayout({
      modelId: p.modelId,
      profileId: p.profileId,
      tokens: p.tokens,
      includeMtp: p.includeMtp,
      compressorPolicy: p.compressorPolicy,
      byteMode: p.byteMode,
      customWireFactor: p.customWireFactor,
      inFlightTokens: p.inFlightTokens,
    });
    var bytes = layout.selectedTotalBytes;
    var usableHbmGB = Math.max(0, numberOr(p.usableHbmGB, 0));
    var usableG2TB = Math.max(0, numberOr(p.usableG2TB, 0));
    var rawCmxPB = Math.max(0, numberOr(p.rawCmxPB, 0));
    var cmxUsableFraction = clamp(p.cmxUsableFraction, 0, 1);
    var binaryUnits = p.capacityUnitMode === "binary";
    var hbmBytes = usableHbmGB * (binaryUnits ? GiB : GB);
    var g2Bytes = usableG2TB * (binaryUnits ? 1024 ** 4 : 1e12);
    var rawCmxBytes = rawCmxPB * (binaryUnits ? 1024 ** 5 : 1e15);
    var usableCmxBytes = rawCmxBytes * cmxUsableFraction;
    var growingShardRanks = Math.max(
      1,
      Math.floor(numberOr(p.growingShardRanks, 1))
    );
    var stateShardRanks = Math.max(
      1,
      Math.floor(numberOr(p.stateShardRanks, 1))
    );
    var poolCopies = Math.max(
      1,
      Math.floor(numberOr(p.poolCopies, 1))
    );
    var activePrefixes = Math.max(
      0,
      Math.floor(numberOr(p.activePrefixes, 1))
    );
    var hbmSessionBytes =
      layout.selectedGrowingBytes == null || layout.selectedStateBytes == null
        ? null
        : layout.selectedGrowingBytes / growingShardRanks +
          layout.selectedStateBytes / stateShardRanks;
    var poolSessionBytes = bytes == null ? null : bytes * poolCopies;
    var rawWorkingSetBytes =
      poolSessionBytes == null || cmxUsableFraction === 0
        ? null
        : (activePrefixes * poolSessionBytes) / cmxUsableFraction;
    return {
      layout: layout,
      usableHbmGB: usableHbmGB,
      usableG2TB: usableG2TB,
      rawCmxPB: rawCmxPB,
      cmxUsableFraction: cmxUsableFraction,
      capacityUnitMode: binaryUnits ? "binary" : "decimal",
      growingShardRanks: growingShardRanks,
      stateShardRanks: stateShardRanks,
      poolCopies: poolCopies,
      activePrefixes: activePrefixes,
      hbmSessionBytes: hbmSessionBytes,
      poolSessionBytes: poolSessionBytes,
      rawWorkingSetBytes: rawWorkingSetBytes,
      hbmSessionsPerGpu:
        hbmSessionBytes > 0 ? Math.floor(hbmBytes / hbmSessionBytes) : null,
      g2SessionsPerRack:
        poolSessionBytes > 0 ? Math.floor(g2Bytes / poolSessionBytes) : null,
      rawCmxSessionsPerPod:
        poolSessionBytes > 0
          ? Math.floor(rawCmxBytes / poolSessionBytes)
          : null,
      usableCmxSessionsPerPod:
        poolSessionBytes > 0
          ? Math.floor(usableCmxBytes / poolSessionBytes)
          : null,
    };
  }

  function relativeIncrease(before, after) {
    if (before == null || after == null) return null;
    if (before === 0) return after === 0 ? 0 : Infinity;
    return after / before - 1;
  }

  function hitComparisonScenario(p) {
    var activeSessions = Math.max(
      0,
      Math.floor(numberOr(p.activeSessions, 0))
    );
    var retentionSeconds = Math.max(0, numberOr(p.retentionSeconds, 0));
    var poolCopies = Math.max(
      1,
      Math.floor(numberOr(p.poolCopies, 1))
    );
    var usableFraction = clamp(p.usableFraction, 0, 1);
    var includeRetainedWrites = !!p.includeRetainedWrites;

    function atHit(hitRate) {
      var prefill = prefillScenario(
        Object.assign({}, p, {
          hitRate: hitRate,
          loadMode: "compute",
        })
      );
      var hotPrefixBytes =
        prefill.matchedLayout.selectedTotalBytes == null
          ? null
          : activeSessions * prefill.matchedLayout.selectedTotalBytes;
      var retainedWriteBytes =
        !includeRetainedWrites
          ? 0
          : prefill.requiredWrite == null
          ? null
          : prefill.requiredWrite * retentionSeconds;
      var logicalRetainedBytes = addBytes(
        hotPrefixBytes,
        retainedWriteBytes
      );
      var rawStorageBytes =
        logicalRetainedBytes == null || usableFraction === 0
          ? null
          : (logicalRetainedBytes * poolCopies) / usableFraction;
      return {
        prefill: prefill,
        hotPrefixBytes: hotPrefixBytes,
        retainedWriteBytes: retainedWriteBytes,
        logicalRetainedBytes: logicalRetainedBytes,
        rawStorageBytes: rawStorageBytes,
      };
    }

    var low = atHit(clamp(p.hitRateLow, 0, 1));
    var high = atHit(clamp(p.hitRateHigh, 0, 1));
    return {
      activeSessions: activeSessions,
      retentionSeconds: retentionSeconds,
      poolCopies: poolCopies,
      usableFraction: usableFraction,
      includeRetainedWrites: includeRetainedWrites,
      low: low,
      high: high,
      storageCostIncrease: relativeIncrease(
        low.rawStorageBytes,
        high.rawStorageBytes
      ),
      computeReqIncrease: relativeIncrease(
        low.prefill.offeredReqsPool,
        high.prefill.offeredReqsPool
      ),
      readIncrease: relativeIncrease(
        low.prefill.requiredRead,
        high.prefill.requiredRead
      ),
      writeIncrease: relativeIncrease(
        includeRetainedWrites ? low.prefill.requiredWrite : null,
        includeRetainedWrites ? high.prefill.requiredWrite : null
      ),
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
    if (Math.abs(value) >= 10000) return value.toFixed(0) + " GB/s";
    if (Math.abs(value) >= 1000) return value.toFixed(1) + " GB/s";
    if (Math.abs(value) < 1) return value.toFixed(5) + " GB/s";
    return value.toFixed(2) + " GB/s";
  }

  function fmtCapacity(bytes) {
    if (bytes == null) return "N/A";
    if (Number.isNaN(bytes)) return "N/A";
    if (!Number.isFinite(bytes)) return "∞";
    if (Math.abs(bytes) >= PiB) return (bytes / PiB).toFixed(3) + " PiB";
    if (Math.abs(bytes) >= TiB) return (bytes / TiB).toFixed(2) + " TiB";
    return fmtGiB(bytes);
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
    TiB: TiB,
    PiB: PiB,
    GB: GB,
    MODELS: MODELS,
    PROFILES: PROFILES,
    AGENTIC_PRESETS: AGENTIC_PRESETS,
    profileFor: profileFor,
    profileOptions: profileOptions,
    sessionLayout: sessionLayout,
    matchedTokens: matchedTokens,
    prefillScenario: prefillScenario,
    capacityScenario: capacityScenario,
    hitComparisonScenario: hitComparisonScenario,
    fmtGiB: fmtGiB,
    fmtGBs: fmtGBs,
    fmtCapacity: fmtCapacity,
    fmtRate: fmtRate,
  };

  root.CmxSim = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
