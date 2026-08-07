package router

// P7.1 测量基座:Go 探针。
// 经 bufconn fake worker/agent 驱动真实 handler + PQScheduler,采集:
//   - e2e 时延(POST → 完整响应,含 mock generate;不是 TTFT——首 token 与
//     完整响应在 mock 下不可分,勿当 TTFT 喂 P7.2/P7.4 模型)
//   - 队列等待采样(PQScheduler.WaitSamples)
//   - 选路模式分布(Server.ModeCounts)
// 设 LAKE_BENCH_OUT 时写 JSONL(schema 见 bench/README.md);否则只跑健全性断言。

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"sync"
	"testing"
	"time"

	lakepb "github.com/chengda-wu/lake/go/pb"
)

type benchRecord struct {
	Name      string             `json:"name"`
	Lang      string             `json:"lang"`
	Phase     string             `json:"phase"`
	Env       map[string]string  `json:"env"`
	LatencyMs map[string]float64 `json:"latency_ms,omitempty"`
	Counters  map[string]uint64  `json:"counters,omitempty"`
}

func benchEnv() map[string]string {
	return map[string]string{
		"transport": "bufconn(loopback)",
		"compute":   "mock-worker",
		"note":      "原型相对值;真硬件绝对值校准 defer(issue #61)",
	}
}

// percentile 对毫秒采样求 p50/p99(升序排序后线性取值)。
func percentile(samples []float64, q float64) float64 {
	if len(samples) == 0 {
		return 0
	}
	sort.Float64s(samples)
	idx := int(q * float64(len(samples)-1))
	return samples[idx]
}

func appendJSONL(t *testing.T, rec benchRecord) {
	t.Helper()
	out := os.Getenv("LAKE_BENCH_OUT")
	if out == "" {
		return
	}
	f, err := os.OpenFile(out, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatalf("open LAKE_BENCH_OUT: %v", err)
	}
	defer f.Close()
	b, err := json.Marshal(rec)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Write(append(b, '\n')); err != nil {
		t.Fatal(err)
	}
}

// TestBenchP71RouterLatency 并发打满小容量调度器,测 e2e + 队列等待 + 模式分布。
func TestBenchP71RouterLatency(t *testing.T) {
	const (
		nReq        = 96
		maxInFlight = 4
		workerDelay = 2 * time.Millisecond
	)
	worker := fakeWorker{generate: func(ctx context.Context, req *lakepb.GenerateRequest) (*lakepb.GenerateResponse, error) {
		select {
		case <-time.After(workerDelay):
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		return &lakepb.GenerateResponse{
			RequestId: req.GetRequestId(),
			Mode:      req.GetExecMode(),
		}, nil
	}}
	agent := fakeAgent{dispatch: func(context.Context, *lakepb.DispatchRequest) (*lakepb.Ack, error) {
		return &lakepb.Ack{Ok: true}, nil
	}}
	srv := testServer(t, agent, worker)
	srv.cfg.MaxInFlight = maxInFlight
	srv.sched.SetMaxInFlight(maxInFlight)

	start := time.Now()
	var wg sync.WaitGroup
	e2e := make([]float64, nReq)
	errs := make([]error, nReq)
	for i := 0; i < nReq; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			t0 := time.Now()
			rec := postChat(t, srv, fmt.Sprintf(`{"model":"m","messages":[{"role":"u","content":"%d hello world prefix tokens here"}],"max_tokens":4}`, i))
			e2e[i] = float64(time.Since(t0).Microseconds()) / 1000.0
			if rec.Code != 200 {
				errs[i] = fmt.Errorf("status=%d body=%s", rec.Code, rec.Body.String())
			}
		}(i)
	}
	wg.Wait()
	wall := time.Since(start)
	for i, err := range errs {
		if err != nil {
			t.Fatalf("req %d: %v", i, err)
		}
	}

	waits := srv.sched.WaitSamples()
	waitMs := make([]float64, len(waits))
	for i, w := range waits {
		waitMs[i] = float64(w.Microseconds()) / 1000.0
	}
	qps := float64(nReq) / wall.Seconds()

	appendJSONL(t, benchRecord{
		Name:  "router_e2e_latency",
		Lang:  "go",
		Phase: "P7.1",
		Env:   benchEnv(),
		LatencyMs: map[string]float64{
			"p50": percentile(append([]float64(nil), e2e...), 0.50),
			"p99": percentile(append([]float64(nil), e2e...), 0.99),
		},
		Counters: map[string]uint64{"requests": nReq},
	})
	appendJSONL(t, benchRecord{
		Name:  "router_queue_wait",
		Lang:  "go",
		Phase: "P7.1",
		Env:   benchEnv(),
		LatencyMs: map[string]float64{
			"p50": percentile(append([]float64(nil), waitMs...), 0.50),
			"p99": percentile(append([]float64(nil), waitMs...), 0.99),
		},
		Counters: map[string]uint64{"samples": uint64(len(waits))},
	})
	modeCounters := map[string]uint64{"requests": nReq}
	for mode, n := range srv.ModeCounts() {
		modeCounters["mode_"+mode] = n
	}
	appendJSONL(t, benchRecord{
		Name:     "router_mode_distribution",
		Lang:     "go",
		Phase:    "P7.1",
		Env:      benchEnv(),
		Counters: modeCounters,
	})
	t.Logf("e2e p50=%.2fms p99=%.2fms; queue-wait p50=%.2fms p99=%.2fms; %.0f req/s; modes=%v",
		percentile(append([]float64(nil), e2e...), 0.50),
		percentile(append([]float64(nil), e2e...), 0.99),
		percentile(append([]float64(nil), waitMs...), 0.50),
		percentile(append([]float64(nil), waitMs...), 0.99),
		qps, srv.ModeCounts())
}
