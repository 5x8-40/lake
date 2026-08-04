package router

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// P6.4 判据(issue #52):反压下 priority 排序 + 抢占语义;无 shedding 越界。

type pqProbe struct {
	mu      sync.Mutex
	started []string // exec 启动顺序
}

func (p *pqProbe) record(name string) {
	p.mu.Lock()
	p.started = append(p.started, name)
	p.mu.Unlock()
}

func (p *pqProbe) order() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]string(nil), p.started...)
}

func runSched(t *testing.T, s *PQScheduler) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go s.Run(ctx)
}

// 阻塞型 exec:启动时记名,直到 release 关闭或 ctx 取消。
func blockingExec(probe *pqProbe, name string, release <-chan struct{}) func(context.Context) error {
	return func(ctx context.Context) error {
		probe.record(name)
		select {
		case <-release:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func submitInBackground(s *PQScheduler, priority int, model string, exec func(context.Context) error) chan error {
	done := make(chan error, 1)
	go func() { done <- s.Submit(context.Background(), priority, model, exec) }()
	return done
}

func waitStarted(t *testing.T, probe *pqProbe, want int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if len(probe.order()) >= want {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timeout waiting for %d started execs, got %v", want, probe.order())
}

// waitQueueLen 等排队长度到位——保证多次 Submit 的入队(及 seq)顺序确定。
func waitQueueLen(t *testing.T, s *PQScheduler, want int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if int(s.LoadSnapshot("r").GetQueueLen()) == want {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timeout waiting for queue_len=%d", want)
}

// 排序:priority 大者先执行;同 priority FIFO。
func TestPQPriorityOrdering(t *testing.T) {
	s := NewPQScheduler(1, time.Second)
	runSched(t, s)
	probe := &pqProbe{}
	release := make(chan struct{})

	// 首个请求占住唯一执行位(priority=10,避免后续入队触发抢占)
	firstDone := submitInBackground(s, 10, "m", blockingExec(probe, "first", release))
	waitStarted(t, probe, 1)

	// 排队:mid(p=5) → low(p=1) → mid2(p=5,同优先级后入队);逐个等入队保证 seq 序
	midDone := submitInBackground(s, 5, "m", blockingExec(probe, "mid", release))
	waitQueueLen(t, s, 1)
	lowDone := submitInBackground(s, 1, "m", blockingExec(probe, "low", release))
	waitQueueLen(t, s, 2)
	mid2Done := submitInBackground(s, 5, "m", blockingExec(probe, "mid2", release))
	waitQueueLen(t, s, 3)
	close(release) // 全部放行

	for _, d := range []chan error{firstDone, midDone, lowDone, mid2Done} {
		if err := <-d; err != nil {
			t.Fatalf("submit err = %v", err)
		}
	}
	got := probe.order()
	want := []string{"first", "mid", "mid2", "low"}
	if len(got) != len(want) {
		t.Fatalf("order = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("order = %v, want %v(priority 大者先,同优先级 FIFO)", got, want)
		}
	}
}

// 抢占:在途低优先级被高优先级取消(errPreempted)→ 重排 → 高先完成 → 低重跑成功。
// 被抢占者不丢(无 shedding),代价是一次重跑(KV 留池,重跑命中前缀)。
func TestPQPreemptionRequeuesVictim(t *testing.T) {
	s := NewPQScheduler(1, time.Second)
	runSched(t, s)
	probe := &pqProbe{}
	highRelease := make(chan struct{})
	var lowCalls atomic.Int32

	lowDone := submitInBackground(s, 1, "m", func(ctx context.Context) error {
		call := lowCalls.Add(1)
		probe.record("low")
		if call == 1 {
			<-ctx.Done() // 首次在途:等待被抢占
			return ctx.Err()
		}
		return nil // 重跑:直接完成
	})
	waitStarted(t, probe, 1)

	highDone := submitInBackground(s, 10, "m", blockingExec(probe, "high", highRelease))
	waitStarted(t, probe, 2) // high 已启动(low 被抢占重排)
	close(highRelease)

	if err := <-highDone; err != nil {
		t.Fatalf("high err = %v", err)
	}
	if err := <-lowDone; err != nil {
		t.Fatalf("low err = %v(被抢占者应重跑成功,不丢)", err)
	}
	if got := lowCalls.Load(); got != 2 {
		t.Fatalf("low exec calls = %d, want 2(抢占→重排→重跑)", got)
	}
	got := probe.order()
	want := []string{"low", "high", "low"}
	if len(got) != len(want) {
		t.Fatalf("order = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("order = %v, want %v(抢占后高优先级先完成,低重跑)", got, want)
		}
	}
}

// 同优先级不抢占。
func TestPQNoPreemptSamePriority(t *testing.T) {
	s := NewPQScheduler(1, time.Second)
	runSched(t, s)
	probe := &pqProbe{}
	release := make(chan struct{})

	firstDone := submitInBackground(s, 5, "m", blockingExec(probe, "first", release))
	waitStarted(t, probe, 1)
	secondDone := submitInBackground(s, 5, "m", blockingExec(probe, "second", release))
	select {
	case <-secondDone:
		t.Fatal("second 不应在同优先级下抢占 first")
	case <-time.After(30 * time.Millisecond):
	}
	close(release)
	for _, d := range []chan error{firstDone, secondDone} {
		if err := <-d; err != nil {
			t.Fatalf("err = %v", err)
		}
	}
}

// 无 shedding:队列无界,过载时全部请求最终完成,无一被拒。
func TestPQNoSheddingUnderOverload(t *testing.T) {
	s := NewPQScheduler(2, time.Second)
	runSched(t, s)
	const n = 20
	var completed atomic.Int32
	dones := make([]chan error, 0, n)
	for i := 0; i < n; i++ {
		dones = append(dones, submitInBackground(s, i%3, "m", func(context.Context) error {
			completed.Add(1)
			return nil
		}))
	}
	for i, d := range dones {
		if err := <-d; err != nil {
			t.Fatalf("req %d err = %v(任何拒绝都是 shedding 越界)", i, err)
		}
	}
	if got := completed.Load(); got != n {
		t.Fatalf("completed = %d, want %d", got, n)
	}
}

// 背压:触硬配额的 model 新启动暂停(不丢);他模型不受影响;信号过期自动恢复。
func TestPQBackpressurePausesNewStarts(t *testing.T) {
	const bpTTL = 100 * time.Millisecond
	s := NewPQScheduler(1, bpTTL)
	runSched(t, s)
	probe := &pqProbe{}

	s.SetBackpressure("m", time.Now())
	mDone := submitInBackground(s, 5, "m", func(context.Context) error {
		probe.record("m")
		return nil
	})
	xDone := submitInBackground(s, 5, "x", func(context.Context) error {
		probe.record("x")
		return nil
	})
	// 背压中的 m 不得启动;x 正常执行
	if err := <-xDone; err != nil {
		t.Fatalf("x err = %v", err)
	}
	select {
	case <-mDone:
		t.Fatal("背压中的 model m 不应启动")
	case <-time.After(30 * time.Millisecond):
	}
	// TTL 到期 → m 自动恢复执行(排队等待,不丢)
	if err := <-mDone; err != nil {
		t.Fatalf("m err = %v(背压解除后应执行,不丢)", err)
	}
	got := probe.order()
	if len(got) != 2 || got[0] != "x" || got[1] != "m" {
		t.Fatalf("order = %v, want [x m](背压 model 推迟到他模型之后)", got)
	}
}

// LoadSnapshot:queue_len/in_flight/remaining_cap 反映调度器状态。
func TestPQLoadSnapshot(t *testing.T) {
	s := NewPQScheduler(1, time.Second)
	runSched(t, s)
	probe := &pqProbe{}
	release := make(chan struct{})
	defer close(release)

	_ = submitInBackground(s, 10, "m", blockingExec(probe, "a", release))
	waitStarted(t, probe, 1)
	_ = submitInBackground(s, 1, "m", blockingExec(probe, "b", release))
	_ = submitInBackground(s, 1, "m", blockingExec(probe, "c", release))
	time.Sleep(20 * time.Millisecond)

	snap := s.LoadSnapshot("router")
	if snap.GetInFlight() != 1 || snap.GetQueueLen() != 2 || snap.GetRemainingCap() != 0 {
		t.Fatalf("snapshot = %+v, want in_flight=1 queue=2 remaining=0", snap)
	}
}

// 客户端取消:排队中摘除,不占容量;不影响其他请求。
func TestPQClientCancelDequeues(t *testing.T) {
	s := NewPQScheduler(1, time.Second)
	runSched(t, s)
	probe := &pqProbe{}
	release := make(chan struct{})
	defer close(release)

	_ = submitInBackground(s, 10, "m", blockingExec(probe, "runner", release))
	waitStarted(t, probe, 1)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- s.Submit(ctx, 1, "m", blockingExec(probe, "quitter", release)) }()
	time.Sleep(20 * time.Millisecond)
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("quitter err = %v, want context.Canceled", err)
	}
	snap := s.LoadSnapshot("router")
	if snap.GetQueueLen() != 0 {
		t.Fatalf("queue_len = %d, want 0(取消者已摘除)", snap.GetQueueLen())
	}
}

// review #56 回归:客户端在途取消 → errAbandoned 取消 exec,不重排不投递——
// 防无人等待的僵尸 Generate(若被当抢占重排,会为一个已断开的客户端再算一遍)。
func TestPQClientCancelInFlightCancelsExec(t *testing.T) {
	s := NewPQScheduler(1, time.Second)
	runSched(t, s)
	var calls atomic.Int32
	causeCh := make(chan error, 1)

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- s.Submit(ctx, 5, "m", func(execCtx context.Context) error {
			calls.Add(1)
			<-execCtx.Done()
			causeCh <- context.Cause(execCtx)
			return execCtx.Err()
		})
	}()
	waitFor(t, 2*time.Second, "exec started", func() bool { return calls.Load() == 1 })
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("submit err = %v, want context.Canceled", err)
	}
	if cause := <-causeCh; !errors.Is(cause, errAbandoned) {
		t.Fatalf("exec cause = %v, want errAbandoned", cause)
	}
	// 不重排:exec 只能被调一次;调度器队列应为空(无人等待的残骸不得复活)
	waitFor(t, 2*time.Second, "drained", func() bool {
		snap := s.LoadSnapshot("r")
		return snap.GetQueueLen() == 0 && snap.GetInFlight() == 0
	})
	time.Sleep(20 * time.Millisecond)
	if got := calls.Load(); got != 1 {
		t.Fatalf("exec calls = %d, want 1(abandoned 不重排)", got)
	}
}

// review #56 回归:背压中的高优请求不抢占在途低优——它自己也启不来,
// 踢掉在途只会空槽 + 无谓重算。
func TestPQNoPreemptWhileBackpressured(t *testing.T) {
	const bpTTL = 150 * time.Millisecond
	s := NewPQScheduler(1, bpTTL)
	runSched(t, s)
	probe := &pqProbe{}
	release := make(chan struct{})
	var lowCancelled atomic.Bool

	lowDone := submitInBackground(s, 1, "x", func(ctx context.Context) error {
		probe.record("low")
		select {
		case <-release:
			return nil
		case <-ctx.Done():
			lowCancelled.Store(true)
			return ctx.Err()
		}
	})
	waitStarted(t, probe, 1)

	// 背压中的 model "m" 高优入队:不得踢掉在途的 low
	s.SetBackpressure("m", time.Now())
	highDone := submitInBackground(s, 10, "m", func(context.Context) error {
		probe.record("high")
		return nil
	})
	time.Sleep(30 * time.Millisecond)
	if lowCancelled.Load() {
		t.Fatal("背压中的新请求不应抢占在途低优")
	}
	close(release) // low 正常完成
	if err := <-lowDone; err != nil {
		t.Fatalf("low err = %v", err)
	}
	// bp 过期后 high 正常执行(排队等待,不丢)
	if err := <-highDone; err != nil {
		t.Fatalf("high err = %v", err)
	}
}
