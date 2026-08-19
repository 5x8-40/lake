/**
 * User-provided APC measurements and interpretable regression helpers.
 *
 * Device names were not included in the source comments, so they remain
 * device-a/device-b. N is measured in K tokens, exactly as in the tables.
 */
(function (root) {
  "use strict";

  var GRID = [
    [0.56, 4], [0.56, 16], [0.56, 64], [0.56, 256], [0.56, 1024],
    [0.90, 4], [0.90, 16], [0.90, 64], [0.90, 256], [0.90, 1024],
    [0.99, 4], [0.99, 16], [0.99, 64], [0.99, 256], [0.99, 1024],
  ];

  var METRICS = {
    ttft: { id: "ttft", label: "TTFT", unit: "s" },
    readMB: { id: "readMB", label: "单层读 KV", unit: "MB" },
    readGBs: { id: "readGBs", label: "读带宽", unit: "GB/s" },
    writeGBs: { id: "writeGBs", label: "写带宽", unit: "GB/s" },
    newKv1hTB: { id: "newKv1hTB", label: "1H 新增 KV", unit: "TB" },
    processed1hTB: {
      id: "processed1hTB",
      label: "1H 处理总量",
      unit: "TB",
    },
  };

  var DEVICES = [
    {
      id: "device-a",
      label: "设备 A",
      noteId: 3702930653,
      sourceUrl:
        "https://gitlab.com/BeeBreeze/lake/-/merge_requests/7#note_3702930653",
      models: [
        {
          id: "deepseek-v3.2-671b",
          label: "DeepSeek-V3.2-671B (DSA)",
          topology: "64 卡 · gbs=64",
          ttft: [
            0.37, 1.61, 8.64, 70.1, 856.44,
            0.09, 0.37, 1.96, 15.93, 192.98,
            0.02, 0.04, 0.2, 1.59, 19.3,
          ],
          readMB: [
            1.54, 6.16, 24.64, 98.56, 394.24,
            2.48, 9.9, 39.6, 158.4, 633.6,
            2.72, 10.89, 43.56, 174.24, 696.96,
          ],
          readGBs: [
            0.252, 0.23, 0.171, 0.084, 0.027,
            1.667, 1.625, 1.207, 0.594, 0.196,
            7.748, 14.63, 13.215, 6.529, 2.153,
          ],
          writeGBs: [
            0.198, 0.181, 0.134, 0.066, 0.022,
            0.185, 0.181, 0.134, 0.066, 0.022,
            0.078, 0.148, 0.133, 0.066, 0.022,
          ],
          newKv1hTB: [
            0.695, 0.636, 0.472, 0.232, 0.076,
            0.651, 0.635, 0.472, 0.232, 0.076,
            0.275, 0.52, 0.469, 0.232, 0.076,
          ],
          processed1hTB: [
            1.58, 1.45, 1.07, 0.53, 0.17,
            6.51, 6.35, 4.72, 2.32, 0.76,
            27.5, 52.0, 46.9, 23.2, 7.6,
          ],
        },
        {
          id: "qwen3-235b",
          label: "Qwen3-235B (GQA)",
          topology: "8 卡 · gbs=8",
          ttft: [
            0.2, 1.32, 13.44, 184.3, 2825.74,
            0.05, 0.3, 3.06, 41.89, 642.22,
            0.03, 0.05, 0.31, 4.19, 64.22,
          ],
          readMB: [
            2.24, 8.96, 35.84, 143.36, 573.44,
            3.6, 14.4, 57.6, 230.4, 921.6,
            3.96, 15.84, 63.36, 253.44, 1013.76,
          ],
          readGBs: [
            1.013, 0.622, 0.245, 0.071, 0.019,
            6.229, 4.4, 1.73, 0.505, 0.132,
            14.075, 30.72, 19.033, 5.555, 1.449,
          ],
          writeGBs: [
            0.796, 0.489, 0.192, 0.056, 0.015,
            0.692, 0.489, 0.192, 0.056, 0.015,
            0.142, 0.31, 0.192, 0.056, 0.015,
          ],
          newKv1hTB: [
            2.798, 1.719, 0.676, 0.197, 0.051,
            2.433, 1.719, 0.676, 0.197, 0.051,
            0.5, 1.091, 0.676, 0.197, 0.051,
          ],
          processed1hTB: [
            6.36, 3.91, 1.54, 0.45, 0.12,
            24.33, 17.19, 6.76, 1.97, 0.51,
            50.0, 109.1, 67.6, 19.7, 5.1,
          ],
          noCacheTTFT: [0.47, 3.03, 30.67, 419.35, "OOM"],
          noCacheTPS: [8740.85, 5402.64, 2136.73, 625.12, null],
        },
        {
          id: "deepseek-v4-pro",
          label: "DeepSeek-V4-Pro",
          topology: "64 卡 · gbs=64",
          ttft: [
            0.36, 1.46, 6.25, 31.6, 237.86,
            0.09, 0.33, 1.42, 7.18, 53.62,
            0.04, 0.05, 0.15, 0.72, 5.27,
          ],
          readMB: [
            0.39, 1.44, 5.64, 22.44, 89.64,
            0.62, 2.31, 9.06, 36.06, 144.06,
            0.68, 2.54, 9.96, 39.66, 158.46,
          ],
          readGBs: [
            0.0352, 0.0287, 0.026, 0.02, 0.0112,
            0.233, 0.2031, 0.1839, 0.1459, 0.0796,
            0.5799, 1.5007, 1.9484, 1.6042, 0.8899,
          ],
          writeGBs: [
            0.027, 0.024, 0.021, 0.017, 0.009,
            0.026, 0.024, 0.021, 0.017, 0.009,
            0.006, 0.016, 0.021, 0.017, 0.009,
          ],
          newKv1hTB: [
            0.097, 0.084, 0.075, 0.059, 0.031,
            0.09, 0.084, 0.075, 0.059, 0.032,
            0.02, 0.055, 0.072, 0.059, 0.032,
          ],
          processed1hTB: [
            0.22, 0.19, 0.17, 0.13, 0.07,
            0.9, 0.84, 0.75, 0.59, 0.32,
            2.0, 5.5, 7.2, 5.9, 3.2,
          ],
        },
      ],
    },
    {
      id: "device-b",
      label: "设备 B",
      noteId: 3702931264,
      sourceUrl:
        "https://gitlab.com/BeeBreeze/lake/-/merge_requests/7#note_3702931264",
      models: [
        {
          id: "deepseek-v3.2-671b",
          label: "DeepSeek-V3.2-671B (DSA)",
          topology: "64 卡 · gbs=64",
          ttft: [
            0.033, 0.131, 0.615, 3.99, 40.522,
            0.011, 0.032, 0.141, 0.908, 9.211,
            0.004, 0.007, 0.017, 0.092, 0.922,
          ],
          readMB: [
            1.54, 6.16, 24.64, 98.56, 394.24,
            2.48, 9.9, 39.6, 158.4, 633.6,
            2.72, 10.89, 43.56, 174.24, 696.96,
          ],
          readGBs: [
            6.334, 5.911, 4.287, 2.022, 0.649,
            35.407, 40.435, 30.125, 14.29, 4.59,
            56.743, 189.136, 309.354, 155.896, 50.455,
          ],
          writeGBs: [
            4.977, 4.644, 3.368, 1.589, 0.51,
            3.934, 4.493, 3.347, 1.588, 0.51,
            0.573, 1.91, 3.125, 1.575, 0.51,
          ],
          newKv1hTB: [
            17.496, 16.328, 11.841, 5.586, 1.793,
            13.831, 15.795, 11.767, 5.582, 1.793,
            2.015, 6.716, 10.986, 5.536, 1.792,
          ],
          processed1hTB: [
            39.76, 37.11, 26.91, 12.7, 4.08,
            138.31, 157.95, 117.67, 55.82, 17.93,
            201.5, 671.6, 1098.6, 553.6, 179.2,
          ],
        },
        {
          id: "qwen3-235b",
          label: "Qwen3-235B (GQA)",
          topology: "8 卡 · gbs=8",
          ttft: [
            0.018, 0.054, 0.558, 7.871, 125.673,
            0.008, 0.023, 0.131, 1.789, 28.563,
            0.003, 0.006, 0.02, 0.189, 2.858,
          ],
          readMB: [
            2.24, 8.96, 35.84, 143.36, 573.44,
            3.6, 14.4, 57.6, 230.4, 921.6,
            3.96, 15.84, 63.36, 253.44, 1013.76,
          ],
          readGBs: [
            11.204, 15.257, 5.917, 1.674, 0.419,
            42.464, 57.603, 40.557, 11.835, 2.963,
            91.55, 253.218, 284.759, 123.367, 32.574,
          ],
          writeGBs: [
            8.803, 11.988, 4.649, 1.315, 0.329,
            4.718, 6.4, 4.506, 1.315, 0.329,
            0.925, 2.558, 2.876, 1.246, 0.329,
          ],
          newKv1hTB: [
            30.947, 42.145, 16.344, 4.624, 1.157,
            16.588, 22.501, 15.843, 4.623, 1.157,
            3.251, 8.992, 10.112, 4.381, 1.157,
          ],
          processed1hTB: [
            70.33, 95.78, 37.15, 10.51, 2.63,
            165.88, 225.01, 158.43, 46.23, 11.57,
            325.1, 899.2, 1011.2, 438.1, 115.7,
          ],
        },
        {
          id: "deepseek-v4-pro",
          label: "DeepSeek-V4-Pro",
          topology: "64 卡 · gbs=64",
          ttft: [
            0.032, 0.112, 0.463, 2.133, 13.093,
            0.012, 0.03, 0.106, 0.486, 2.977,
            0.005, 0.008, 0.017, 0.053, 0.299,
          ],
          readMB: [
            0.39, 1.44, 5.64, 22.44, 89.64,
            0.62, 2.31, 9.06, 36.06, 144.06,
            0.68, 2.54, 9.96, 39.66, 158.46,
          ],
          readGBs: [
            0.758, 0.8044, 0.7758, 0.6974, 0.499,
            3.2689, 4.8634, 5.4249, 4.919, 3.5269,
            7.7197, 20.7989, 37.2732, 49.6251, 38.5912,
          ],
          writeGBs: [
            0.312, 0.309, 0.289, 0.249, 0.162,
            0.192, 0.265, 0.286, 0.248, 0.162,
            0.041, 0.104, 0.182, 0.229, 0.161,
          ],
          newKv1hTB: [
            1.097, 1.085, 1.016, 0.874, 0.569,
            0.675, 0.932, 1.005, 0.872, 0.568,
            0.144, 0.366, 0.64, 0.805, 0.566,
          ],
          processed1hTB: [
            2.49, 2.47, 2.31, 1.99, 1.29,
            6.75, 9.32, 10.05, 8.72, 5.68,
            14.4, 36.6, 64.0, 80.5, 56.6,
          ],
        },
      ],
    },
  ];

  function deviceFor(deviceId) {
    return DEVICES.find(function (device) {
      return device.id === deviceId;
    }) || DEVICES[0];
  }

  function modelFor(device, modelId) {
    return device.models.find(function (model) {
      return model.id === modelId;
    }) || device.models[0];
  }

  function pointsFor(model) {
    return GRID.map(function (coordinate, index) {
      var lengthIndex = index % 5;
      return {
        hit: coordinate[0],
        nK: coordinate[1],
        ttft: model.ttft[index],
        readMB: model.readMB[index],
        readGBs: model.readGBs[index],
        writeGBs: model.writeGBs[index],
        newKv1hTB: model.newKv1hTB[index],
        processed1hTB: model.processed1hTB[index],
        noCacheTTFT: model.noCacheTTFT
          ? model.noCacheTTFT[lengthIndex]
          : null,
        noCacheTPS: model.noCacheTPS
          ? model.noCacheTPS[lengthIndex]
          : null,
      };
    });
  }

  function solve3(matrix, vector) {
    var augmented = matrix.map(function (row, index) {
      return row.slice().concat(vector[index]);
    });
    for (var column = 0; column < 3; column += 1) {
      var pivot = column;
      for (var row = column + 1; row < 3; row += 1) {
        if (
          Math.abs(augmented[row][column]) >
          Math.abs(augmented[pivot][column])
        ) {
          pivot = row;
        }
      }
      if (Math.abs(augmented[pivot][column]) < 1e-18) return null;
      if (pivot !== column) {
        var swap = augmented[column];
        augmented[column] = augmented[pivot];
        augmented[pivot] = swap;
      }
      var divisor = augmented[column][column];
      for (var item = column; item < 4; item += 1) {
        augmented[column][item] /= divisor;
      }
      for (var other = 0; other < 3; other += 1) {
        if (other === column) continue;
        var factor = augmented[other][column];
        for (var entry = column; entry < 4; entry += 1) {
          augmented[other][entry] -= factor * augmented[column][entry];
        }
      }
    }
    return augmented.map(function (row) {
      return row[3];
    });
  }

  function fitTtft(points) {
    var scale = 1024;
    var normal = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
    var target = [0, 0, 0];
    points.forEach(function (point) {
      var n = point.nK / scale;
      var miss = 1 - point.hit;
      var features = [1, miss * n, miss * n * n];
      // Minimize relative residuals because TTFT spans several decades.
      var weightSquared = 1 / (point.ttft * point.ttft);
      for (var row = 0; row < 3; row += 1) {
        target[row] +=
          weightSquared * features[row] * point.ttft;
        for (var column = 0; column < 3; column += 1) {
          normal[row][column] +=
            weightSquared * features[row] * features[column];
        }
      }
    });
    var scaled = solve3(normal, target);
    if (!scaled) throw new Error("TTFT fit is singular");
    return {
      c: scaled[0],
      a: scaled[1] / scale,
      b: scaled[2] / (scale * scale),
    };
  }

  function fitThroughOrigin(points, xValue, yKey) {
    var numerator = 0;
    var denominator = 0;
    points.forEach(function (point) {
      var x = xValue(point);
      numerator += x * point[yKey];
      denominator += x * x;
    });
    return denominator === 0 ? null : numerator / denominator;
  }

  function fitRelativeScale(points, basisValue, yKey) {
    var numerator = 0;
    var denominator = 0;
    points.forEach(function (point) {
      var observed = point[yKey];
      var ratio = basisValue(point) / observed;
      numerator += ratio;
      denominator += ratio * ratio;
    });
    return denominator === 0 ? null : numerator / denominator;
  }

  function predictFromCoefficients(coefficients, hitRate, nK) {
    var hit = Math.min(1, Math.max(0, Number(hitRate)));
    var length = Math.max(0, Number(nK));
    var miss = 1 - hit;
    var ttft =
      coefficients.c +
      miss *
        (coefficients.a * length +
          coefficients.b * length * length);
    var readMB = coefficients.k * hit * length;
    var readGBs =
      ttft > 0
        ? (coefficients.dRead * hit * length) / ttft
        : null;
    var writeGBs =
      ttft > 0
        ? (coefficients.dWrite * miss * length) / ttft
        : null;
    var newKv1hTB =
      writeGBs == null ? null : (writeGBs * 3600) / 1024;
    var processed1hTB =
      newKv1hTB == null || miss === 0
        ? null
        : newKv1hTB / miss;
    return {
      hit: hit,
      nK: length,
      ttft: ttft,
      readMB: readMB,
      readGBs: readGBs,
      writeGBs: writeGBs,
      newKv1hTB: newKv1hTB,
      processed1hTB: processed1hTB,
    };
  }

  function qualityFor(observed, predicted) {
    var mean =
      observed.reduce(function (sum, value) {
        return sum + value;
      }, 0) / observed.length;
    var residual = 0;
    var total = 0;
    var relative = 0;
    observed.forEach(function (value, index) {
      var error = predicted[index] - value;
      residual += error * error;
      total += (value - mean) * (value - mean);
      relative += (error / value) * (error / value);
    });
    return {
      r2: total === 0 ? null : 1 - residual / total,
      relativeRmse: Math.sqrt(relative / observed.length),
    };
  }

  function buildFit(deviceId, modelId) {
    var device = deviceFor(deviceId);
    var model = modelFor(device, modelId);
    var points = pointsFor(model);
    var ttft = fitTtft(points);
    var k = fitThroughOrigin(
      points,
      function (point) {
        return point.hit * point.nK;
      },
      "readMB"
    );
    var ttftForPoint = function (point) {
      return predictFromCoefficients(
        {
          c: ttft.c,
          a: ttft.a,
          b: ttft.b,
          k: k,
          dRead: 0,
          dWrite: 0,
        },
        point.hit,
        point.nK
      ).ttft;
    };
    var dRead = fitRelativeScale(
      points,
      function (point) {
        return (point.hit * point.nK) / ttftForPoint(point);
      },
      "readGBs"
    );
    // V4 has a different write representation, so read and write factors
    // are fitted separately instead of assuming symmetric bytes/token.
    var dWrite = fitRelativeScale(
      points,
      function (point) {
        return ((1 - point.hit) * point.nK) / ttftForPoint(point);
      },
      "writeGBs"
    );
    var coefficients = {
      c: ttft.c,
      a: ttft.a,
      b: ttft.b,
      k: k,
      dRead: dRead,
      dWrite: dWrite,
    };
    var predicted = points.map(function (point) {
      return predictFromCoefficients(
        coefficients,
        point.hit,
        point.nK
      );
    });
    var quality = {};
    Object.keys(METRICS).forEach(function (metricId) {
      quality[metricId] = qualityFor(
        points.map(function (point) {
          return point[metricId];
        }),
        predicted.map(function (point) {
          return point[metricId];
        })
      );
    });
    return {
      device: device,
      model: model,
      points: points,
      coefficients: coefficients,
      predicted: predicted,
      quality: quality,
    };
  }

  var api = {
    GRID: GRID,
    METRICS: METRICS,
    DEVICES: DEVICES,
    deviceFor: deviceFor,
    modelFor: modelFor,
    pointsFor: pointsFor,
    buildFit: buildFit,
    predict: function (fit, hitRate, nK) {
      return predictFromCoefficients(
        fit.coefficients,
        hitRate,
        nK
      );
    },
  };

  root.CmxMeasured = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
