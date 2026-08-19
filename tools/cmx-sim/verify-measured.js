"use strict";

const assert = require("node:assert/strict");
const M = require("./measured.js");

function close(actual, expected, tolerance, message) {
  assert.ok(
    Number.isFinite(actual) &&
      Number.isFinite(expected) &&
      Math.abs(actual - expected) <= tolerance,
    `${message}: expected ${expected}, got ${actual}`
  );
}

function relativeClose(actual, expected, tolerance, message) {
  const scale = Math.max(Math.abs(expected), Number.EPSILON);
  close(actual, expected, tolerance * scale, message);
}

assert.equal(M.DEVICES.length, 2, "two source devices");
assert.deepEqual(
  M.DEVICES.map((device) => device.noteId),
  [3702930653, 3702931264],
  "source comment IDs"
);
assert.equal(M.GRID.length, 15, "three hit rates by five lengths");
assert.deepEqual(
  [...new Set(M.GRID.map((row) => row[0]))],
  [0.56, 0.9, 0.99],
  "source hit-rate grid"
);
assert.deepEqual(
  M.GRID.slice(0, 5).map((row) => row[1]),
  [4, 16, 64, 256, 1024],
  "source sequence-length grid"
);

M.DEVICES.forEach((device) => {
  assert.equal(device.models.length, 3, `${device.id} model count`);
  device.models.forEach((model) => {
    const points = M.pointsFor(model);
    assert.equal(points.length, 15, `${device.id}/${model.id} point count`);
    Object.keys(M.METRICS).forEach((metricId) => {
      assert.equal(
        model[metricId].length,
        15,
        `${device.id}/${model.id}/${metricId} value count`
      );
      model[metricId].forEach((value) => {
        assert.ok(
          Number.isFinite(value) && value > 0,
          `${device.id}/${model.id}/${metricId} finite positive source`
        );
      });
    });
  });
});

const deviceA = M.deviceFor("device-a");
const deviceB = M.deviceFor("device-b");
const aDs = M.modelFor(deviceA, "deepseek-v3.2-671b");
const aQwen = M.modelFor(deviceA, "qwen3-235b");
const bQwen = M.modelFor(deviceB, "qwen3-235b");
const bV4 = M.modelFor(deviceB, "deepseek-v4-pro");

assert.equal(aDs.ttft[4], 856.44, "device A DS 56%/1024K sentinel");
assert.equal(aQwen.ttft[4], 2825.74, "device A Qwen 56%/1024K sentinel");
assert.equal(aQwen.noCacheTTFT[4], "OOM", "Qwen no-cache OOM retained");
assert.equal(aQwen.noCacheTPS[4], null, "Qwen no-cache empty TPS retained");
assert.equal(bQwen.ttft[10], 0.003, "device B Qwen 99%/4K sentinel");
assert.equal(bV4.readGBs[13], 49.6251, "device B V4 read-bandwidth sentinel");
assert.equal(bV4.writeGBs[13], 0.229, "device B V4 write-bandwidth sentinel");

const baselineQwenCoefficients = M.buildFit(
  "device-a",
  "qwen3-235b"
).coefficients;
const originalNoCache = aQwen.noCacheTTFT[0];
try {
  aQwen.noCacheTTFT[0] = 999999;
  assert.deepEqual(
    M.buildFit("device-a", "qwen3-235b").coefficients,
    baselineQwenCoefficients,
    "no-cache TTFT is excluded from every fit"
  );
} finally {
  aQwen.noCacheTTFT[0] = originalNoCache;
}

M.DEVICES.forEach((device) => {
  device.models.forEach((model) => {
    const fit = M.buildFit(device.id, model.id);
    Object.entries(fit.coefficients).forEach(([name, value]) => {
      assert.ok(
        Number.isFinite(value),
        `${device.id}/${model.id}/${name} finite coefficient`
      );
    });
    assert.ok(
      fit.quality.ttft.r2 > 0.99,
      `${device.id}/${model.id} TTFT R²`
    );
    assert.ok(
      fit.quality.ttft.relativeRmse < 0.16,
      `${device.id}/${model.id} TTFT relative RMSE`
    );
    assert.ok(
      fit.quality.readMB.r2 > 0.999,
      `${device.id}/${model.id} read-KV R²`
    );
    assert.ok(
      fit.quality.readMB.relativeRmse < 0.05,
      `${device.id}/${model.id} read-KV relative RMSE`
    );
    assert.ok(
      fit.quality.readGBs.r2 > 0.9,
      `${device.id}/${model.id} read-bandwidth R²`
    );
    assert.ok(
      fit.quality.readGBs.relativeRmse < 0.27,
      `${device.id}/${model.id} read-bandwidth relative RMSE`
    );

    Object.values(fit.quality).forEach((quality) => {
      assert.ok(Number.isFinite(quality.r2), "finite R²");
      assert.ok(Number.isFinite(quality.relativeRmse), "finite relative RMSE");
    });

    const predicted = M.predict(fit, 0.9, 64);
    Object.keys(M.METRICS).forEach((metricId) => {
      assert.ok(
        Number.isFinite(predicted[metricId]) && predicted[metricId] > 0,
        `${device.id}/${model.id}/${metricId} finite positive prediction`
      );
    });
    close(
      predicted.ttft,
      fit.coefficients.c +
        0.1 *
          (fit.coefficients.a * 64 +
            fit.coefficients.b * 64 * 64),
      1e-12,
      "TTFT equation"
    );
    close(
      predicted.readMB,
      fit.coefficients.k * 0.9 * 64,
      1e-12,
      "read-KV equation"
    );
    close(
      predicted.readGBs,
      (fit.coefficients.dRead * 0.9 * 64) / predicted.ttft,
      1e-12,
      "read-bandwidth equation"
    );
    close(
      predicted.writeGBs,
      (fit.coefficients.dWrite * 0.1 * 64) / predicted.ttft,
      1e-12,
      "write-bandwidth equation"
    );
    close(
      predicted.newKv1hTB,
      (predicted.writeGBs * 3600) / 1024,
      1e-12,
      "one-hour new-KV identity"
    );
    close(
      predicted.processed1hTB,
      predicted.newKv1hTB / 0.1,
      1e-12,
      "one-hour processed-data identity"
    );
  });
});

const bV4Fit = M.buildFit("device-b", "deepseek-v4-pro");
assert.ok(
  bV4Fit.coefficients.dRead > 2 * bV4Fit.coefficients.dWrite,
  "V4 read/write representations must not be forced symmetric"
);

// The source tables round hour totals. Check the stated arithmetic within the
// precision of those cells; fitted predictions above are exact identities.
M.DEVICES.forEach((device) => {
  device.models.forEach((model) => {
    M.pointsFor(model).forEach((point) => {
      relativeClose(
        point.newKv1hTB,
        (point.writeGBs * 3600) / 1024,
        0.06,
        "source one-hour new-KV rounding"
      );
      relativeClose(
        point.processed1hTB,
        point.newKv1hTB / (1 - point.hit),
        0.04,
        "source one-hour processed-data rounding"
      );
    });
  });
});

console.log("cmx measured-data verification passed");
