/**
 * Vera Rubin × CMX 仿真：KV 体积 + PD 分离下 Prefill 跑满。
 * 浏览器与 Node 共用。GiB = 2^30（对齐 vLLM 附录 9.62 GiB）。
 */
(function (root) {
  "use strict";

  var GiB = 1024 * 1024 * 1024;
  var GB = 1e9;
  var SWA = 128;
  var HEAD_DIM = 512;
  var INDEX_DIM = 128;

  var DTYPE = {
    bf16: { id: "bf16", kv: 2, idx: 2, label: "BF16 KV + BF16 indexer" },
    fp8: { id: "fp8", kv: 1, idx: 1, label: "FP8 KV + FP8 indexer" },
    fp8_fp4: { id: "fp8_fp4", kv: 1, idx: 0.5, label: "FP8 attn KV + FP4 indexer" },
  };

  /**
   * V4-Pro：61 层，前 2 层 HCA，其后 CSA/HCA 交错 → 30 c4a + 31 c128a。
   * V4-Flash：43 层，前 2 层纯 SWA，其后交错 → 21 c4a + 20 c128a。
   * 见 DeepSeek-V4 论文 §4.2.1；体积算术见 vLLM 2026-04-24 附录。
   */
  var MODELS = {
    v4pro: {
      id: "v4pro",
      name: "DeepSeek V4-Pro",
      kind: "v4",
      totalParams: "1.6T",
      nActive: 49e9,
      nC4: 30,
      nC128: 31,
      nSwaOnly: 0,
      indexHeads: 64,
      topk: 1024,
      nQHeads: 128,
      note: "共享 KV（K=V）+ c4a/c128a 压缩。BF16 @ 1,048,576 = 9.62 GiB。",
    },
    v4flash: {
      id: "v4flash",
      name: "DeepSeek V4-Flash",
      kind: "v4",
      totalParams: "284B",
      nActive: 13e9,
      nC4: 21,
      nC128: 20,
      nSwaOnly: 2,
      indexHeads: 64,
      topk: 512,
      nQHeads: 64,
      note: "前 2 层纯 SWA，其余 CSA/HCA 交错。BF16 @ 1M ≈ 6.73 GiB。",
    },
    glm52: {
      id: "glm52",
      name: "GLM-5.2",
      kind: "glm",
      totalParams: "744B-A40B",
      nActive: 40e9,
      nLayers: 79,
      nIndexer: 22,
      mlaElems: 576,
      idxBytesFp8: 132,
      indexHeads: 32,
      topk: 2048,
      note: "全层 MLA 576-d + DSA indexer（IndexShare 不缩小存储）。FP8 @ 1M ≈ 47.3 GiB。",
    },
    k3: {
      id: "k3",
      name: "Kimi K3",
      kind: "k3",
      totalParams: "2.8T / 104B act",
      nActive: 104e9,
      nMla: 24,
      mlaElems: 576,
      kdaBytes: 221.55 * 1024 * 1024,
      nKda: 69,
      kdaHeads: 96,
      kdaDim: 128,
      note: "24 层 Gated MLA 随 token 增长；69 层 KDA 固定 ~221.55 MiB/会话。",
    },
  };

  function dtypeOf(id) {
    return DTYPE[id] || DTYPE.bf16;
  }

  function v4LayerC4(N, kvB, idxB) {
    return (SWA + N / 4) * kvB + (N / 4) * idxB;
  }

  function v4LayerC128(N, kvB) {
    return (SWA + N / 128) * kvB;
  }

  function v4Kv(N, m, dt) {
    var kvB = HEAD_DIM * dt.kv;
    var idxB = INDEX_DIM * dt.idx;
    var c4 = m.nC4 * v4LayerC4(N, kvB, idxB);
    var c128 = m.nC128 * v4LayerC128(N, kvB);
    var swaOnly = m.nSwaOnly * (SWA * kvB);
    return {
      bytes: c4 + c128 + swaOnly,
      kvB: kvB,
      idxB: idxB,
      c4: c4,
      c128: c128,
      swaOnly: swaOnly,
    };
  }

  function v4IncBpt(m, dt) {
    var kvB = HEAD_DIM * dt.kv;
    var idxB = INDEX_DIM * dt.idx;
    return m.nC4 * (kvB / 4 + idxB / 4) + m.nC128 * (kvB / 128);
  }

  function glmKv(N, m, dt, packed) {
    var mlaBpt =
      packed && (dt.id === "fp8" || dt.id === "fp8_fp4")
        ? m.nLayers * 656
        : m.nLayers * m.mlaElems * dt.kv;
    var idxBpt =
      m.nIndexer * (dt.id === "fp8" || dt.id === "fp8_fp4" ? m.idxBytesFp8 : INDEX_DIM * dt.idx);
    return { bytes: N * (mlaBpt + idxBpt), bpt: mlaBpt + idxBpt, mlaB: mlaBpt, idxBpt: idxBpt };
  }

  function k3Kv(N, m, dt) {
    var mlaBpt = m.nMla * m.mlaElems * dt.kv;
    return { bytes: N * mlaBpt + m.kdaBytes, bpt: mlaBpt, kda: m.kdaBytes };
  }

  function kvAt(modelId, N, dtypeId, packed) {
    var m = MODELS[modelId];
    var dt = dtypeOf(dtypeId);
    N = Math.max(0, Number(N) || 0);
    if (m.kind === "v4") return v4Kv(N, m, dt);
    if (m.kind === "glm") return glmKv(N, m, dt, packed);
    return k3Kv(N, m, dt);
  }

  function incBpt(modelId, dtypeId, packed) {
    var m = MODELS[modelId];
    var dt = dtypeOf(dtypeId);
    if (m.kind === "v4") return v4IncBpt(m, dt);
    if (m.kind === "glm") return glmKv(1, m, dt, packed).bpt;
    return k3Kv(1, m, dt).bpt;
  }

  /**
   * 处理一个 unique token、前缀长度 ≈ C 时的 FLOPs。
   * GEMM：2 × N_active（乘加各计 1）。
   * V4 indexer：只在 c4a 层，对 C/4 压缩位打分：2 × nC4 × (C/4) × h × d。
   * GLM DSA indexer：21/22 层对原生 C 打分。
   * K3：MLA 24 层对 C 做 latent 注意力 + KDA O(h d²)。
   */
  function flopsPerUniqueToken(modelId, C, overrides) {
    var m = MODELS[modelId];
    var o = overrides || {};
    var nActive = o.nActive != null ? o.nActive : m.nActive;
    var gemm = 2 * nActive;
    var attn = 0;
    if (m.kind === "v4") {
      var nC4 = o.nC4 != null ? o.nC4 : m.nC4;
      var nC128 = o.nC128 != null ? o.nC128 : m.nC128;
      var ih = o.indexHeads != null ? o.indexHeads : m.indexHeads;
      var topk = o.topk != null ? o.topk : m.topk;
      var nQ = o.nQHeads != null ? o.nQHeads : m.nQHeads;
      var compressed = C / 4;
      attn += 2 * nC4 * compressed * ih * INDEX_DIM;
      attn += 2 * nC4 * topk * nQ * HEAD_DIM;
      attn += 2 * nC128 * Math.min(C / 128, 8192) * nQ * HEAD_DIM;
    } else if (m.kind === "glm") {
      var ihg = o.indexHeads != null ? o.indexHeads : m.indexHeads;
      var nIdx = o.nIndexer != null ? o.nIndexer : m.nIndexer;
      var topkG = o.topk != null ? o.topk : m.topk;
      attn += 2 * nIdx * C * ihg * INDEX_DIM;
      attn += 2 * m.nLayers * topkG * m.mlaElems;
    } else {
      attn += 2 * m.nMla * C * m.mlaElems;
      attn += 2 * m.nKda * m.kdaHeads * m.kdaDim * m.kdaDim;
    }
    return { gemm: gemm, attn: attn, total: gemm + attn };
  }

  function simulate(p) {
    var modelId = p.modelId;
    var m = MODELS[modelId];
    var dt = dtypeOf(p.dtypeId);
    var ctx = Math.max(1, Number(p.ctx) || 1);
    var hit = Math.min(0.999, Math.max(0, Number(p.hit) || 0));
    var extra = Math.max(0, Number(p.extraUnique) || 0);
    var gpus = Math.max(1, Number(p.gpus) || 1);
    var pflops = Number(p.pflops) || 50;
    var util = Math.min(1, Math.max(0.01, Number(p.util) || 0.4));
    var nicGBs = Number(p.nicGBs) || 200;
    var peakFlops = pflops * 1e15;

    var uniqueTok = ctx * (1 - hit) + extra;
    if (uniqueTok < 1) uniqueTok = 1;

    var packed = !!p.mlaPacked;
    var kv = kvAt(modelId, ctx, p.dtypeId, packed);
    var kvHit = kvAt(modelId, ctx * hit, p.dtypeId, packed);
    var bpt = incBpt(modelId, p.dtypeId, packed);
    var flops = flopsPerUniqueToken(modelId, ctx, p.flopsOverride);

    var uniqueTpsGpu = (peakFlops * util) / flops.total;
    var uniqueTps = uniqueTpsGpu * gpus;
    var reqsGpu = uniqueTpsGpu / uniqueTok;
    var reqs = reqsGpu * gpus;

    var writeGpu = uniqueTpsGpu * bpt;
    var write = writeGpu * gpus;
    var extraSess =
      m.kind === "k3" ? m.kdaBytes : 0;
    var readGpu = reqsGpu * kvHit.bytes + reqsGpu * extraSess;
    var read = readGpu * gpus;
    var nic = nicGBs * GB;
    var nicUsedGpu = readGpu + writeGpu;
    var nicPct = (nicUsedGpu / nic) * 100;
    var bottleneck = nicPct >= 100 ? "nic" : "compute";

    var hours = 3600;
    return {
      model: m,
      dtype: dt,
      ctx: ctx,
      hit: hit,
      uniqueTok: uniqueTok,
      gpus: gpus,
      kvBytes: kv.bytes,
      kvGiB: kv.bytes / GiB,
      kvDetail: kv,
      bpt: bpt,
      flops: flops,
      uniqueTpsGpu: uniqueTpsGpu,
      uniqueTps: uniqueTps,
      reqsGpu: reqsGpu,
      reqs: reqs,
      writeGpu: writeGpu,
      write: write,
      readGpu: readGpu,
      read: read,
      writeTBh: (write * hours) / 1e12,
      readTBh: (read * hours) / 1e12,
      nic: nic,
      nicPct: nicPct,
      bottleneck: bottleneck,
      peakFlops: peakFlops,
      util: util,
      extraSess: extraSess,
      kvHitBytes: kvHit.bytes,
      mlaPacked: packed,
    };
  }

  function dtypeSweep(p, ids) {
    ids = ids || ["bf16", "fp8", "fp8_fp4"];
    return ids.map(function (id) {
      return simulate(Object.assign({}, p, { dtypeId: id, mlaPacked: false }));
    });
  }

  function hitSweep(p, hits) {
    return hits.map(function (h) {
      return simulate(Object.assign({}, p, { hit: h }));
    });
  }

  function ctxSweep(p, ctxs) {
    return ctxs.map(function (c) {
      return simulate(Object.assign({}, p, { ctx: c }));
    });
  }

  function fmtGiB(bytes) {
    var g = bytes / GiB;
    if (g >= 100) return g.toFixed(0) + " GiB";
    if (g >= 10) return g.toFixed(2) + " GiB";
    if (g >= 1) return g.toFixed(2) + " GiB";
    if (g >= 0.01) return (g * 1024).toFixed(1) + " MiB";
    return (bytes / 1024).toFixed(0) + " KiB";
  }

  function fmtGBs(bps) {
    var g = bps / GB;
    if (Math.abs(g) >= 100) return g.toFixed(0) + " GB/s";
    if (Math.abs(g) >= 10) return g.toFixed(1) + " GB/s";
    if (Math.abs(g) >= 1) return g.toFixed(2) + " GB/s";
    if (Math.abs(g) >= 0.001) return (g * 1000).toFixed(0) + " MB/s";
    return (bps / 1e3).toFixed(0) + " KB/s";
  }

  function fmtTps(x) {
    if (x >= 1e6) return (x / 1e6).toFixed(2) + " M";
    if (x >= 1e3) return (x / 1e3).toFixed(1) + " k";
    return x.toFixed(1);
  }

  function fmtNum(x, d) {
    if (x >= 1e12) return (x / 1e12).toFixed(d == null ? 2 : d) + " T";
    if (x >= 1e9) return (x / 1e9).toFixed(d == null ? 2 : d) + " G";
    if (x >= 1e6) return (x / 1e6).toFixed(d == null ? 2 : d) + " M";
    if (x >= 1e3) return (x / 1e3).toFixed(d == null ? 1 : d) + " k";
    return x.toFixed(d == null ? 1 : d);
  }

  var HW = {
    gpusRack: 72,
    gpusPod: 1152,
    hbmGpu: 288e9,
    hbmRack: 20.7e12,
    hbmBw: 22e12,
    dramRack: 54e12,
    nicGpu: 200e9,
    pflops: 50,
  };

  function capacity(p) {
    var r = simulate(p);
    var hbmGpu = Number(p.hbmGpu) || HW.hbmGpu;
    var hbmRack = Number(p.hbmRack) || HW.hbmRack;
    var dramRack = Number(p.dramRack) || HW.dramRack;
    var weightFrac = Math.min(0.9, Math.max(0, Number(p.weightFrac) != null ? p.weightFrac : 0.25));
    var cmxBytes = (Number(p.cmxPB) || 1) * 1e15;
    var kv = Math.max(1, r.kvBytes);
    return Object.assign({}, r, {
      sessGpu: Math.floor((hbmGpu * (1 - weightFrac)) / kv),
      sessRackHbm: Math.floor((hbmRack * (1 - weightFrac)) / kv),
      sessDram: Math.floor(dramRack / kv),
      sessCmx: Math.floor(cmxBytes / kv),
      weightFrac: weightFrac,
      cmxBytes: cmxBytes,
      hbmBwDecodeTps: HW.hbmBw / kv,
    });
  }

  var api = {
    GiB: GiB,
    GB: GB,
    HW: HW,
    DTYPE: DTYPE,
    MODELS: MODELS,
    kvAt: kvAt,
    incBpt: incBpt,
    flopsPerUniqueToken: flopsPerUniqueToken,
    simulate: simulate,
    hitSweep: hitSweep,
    ctxSweep: ctxSweep,
    dtypeSweep: dtypeSweep,
    capacity: capacity,
    fmtGiB: fmtGiB,
    fmtGBs: fmtGBs,
    fmtTps: fmtTps,
    fmtNum: fmtNum,
  };

  root.CmxSim = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
