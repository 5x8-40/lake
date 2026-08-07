package router

import (
	"context"
	"sync"
	"testing"
	"time"

	lakepb "github.com/chengda-wu/lake/go/pb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// P6.5 判据(issue #52):扩缩单测 + drain/迁移最小化验证。
// 迁移最小化的环性质在 Rust 侧(shard.rs expand_moves_only_new_interval +
// p65_join_drain_roundtrip_restores_ownership);这里验证决策语义与 CP 接线顺序。

func fastScaler() *Autoscaler {
	return NewAutoscaler(AutoscaleConfig{
		MinNodes:         1,
		MaxNodes:         3,
		ScaleOutQueueLen: 4,
		ScaleInQueueLen:  0,
		SustainPeriods:   3,
		Cooldown:         time.Minute,
	})
}

// 决策:持续超阈值才动作(防抖);cooldown 阻连续动作;min/max 边界;中间值清零计数。
func TestAutoscaleDecisions(t *testing.T) {
	now := time.Now()
	hot := MetricsSnapshot{QueueLen: 5, InFlight: 1}

	// 持续高热:SustainPeriods=3,第 3 次评估才扩
	a := fastScaler()
	if d := a.Evaluate(now, hot, 1); d != DecideNone {
		t.Fatalf("period 1 = %s, want none(未达持续周期)", d)
	}
	if d := a.Evaluate(now, hot, 1); d != DecideNone {
		t.Fatalf("period 2 = %s, want none", d)
	}
	if d := a.Evaluate(now, hot, 1); d != DecideScaleOut {
		t.Fatalf("period 3 = %s, want scale_out", d)
	}
	// cooldown 内:即使持续高热也不动作
	if d := a.Evaluate(now.Add(10*time.Second), hot, 2); d != DecideNone {
		t.Fatalf("cooldown 内 = %s, want none", d)
	}
	// MaxNodes=3:到顶不扩
	if d := a.Evaluate(now.Add(2*time.Minute), hot, 3); d != DecideNone {
		t.Fatalf("max nodes = %s, want none", d)
	}

	// 中间值清零计数(防抖):热-冷-热 不累积
	b := fastScaler()
	b.Evaluate(now, hot, 1)
	b.Evaluate(now, MetricsSnapshot{QueueLen: 2, InFlight: 1}, 1) // 中间值
	b.Evaluate(now, hot, 1)
	if d := b.Evaluate(now, hot, 1); d != DecideNone {
		t.Fatalf("streak 应被中间值清零, = %s, want none", d)
	}

	// 缩容:持续空闲 → scale_in;MinNodes=1 到底不缩
	c := fastScaler()
	idle := MetricsSnapshot{QueueLen: 0, InFlight: 0}
	for i := 0; i < 2; i++ {
		if d := c.Evaluate(now, idle, 2); d != DecideNone {
			t.Fatalf("idle period %d = %s, want none", i, d)
		}
	}
	if d := c.Evaluate(now, idle, 2); d != DecideScaleIn {
		t.Fatalf("idle period 3 = %s, want scale_in", d)
	}
	for i := 0; i < 5; i++ {
		if d := c.Evaluate(now.Add(time.Duration(i+1)*2*time.Minute), idle, 1); d != DecideNone {
			t.Fatalf("min nodes = %s, want none(不缩到 0)", d)
		}
	}
}

// 扩容接线:JoinShardNode 用新节点 ID → 入路由表(Ready)→ 记录决策→Ready 时延。
func TestApplyScaleOutJoinsAndReadies(t *testing.T) {
	var gotNode string
	srv := dialFakeCP(t, fakeCP{
		joinShardNode: func(_ context.Context, req *lakepb.JoinShardNodeRequest) (*lakepb.JoinShardNodeResponse, error) {
			gotNode = req.GetNodeId()
			return &lakepb.JoinShardNodeResponse{
				Ok: true, MigrationCount: 3,
				Map: &lakepb.ShardMap{Generation: 2},
			}, nil
		},
	})
	defer srv.Close()
	srv.nodes = newNodeRegistry("worker-0")

	if err := srv.applyScale(context.Background(), DecideScaleOut); err != nil {
		t.Fatal(err)
	}
	if gotNode != "worker-1" {
		t.Fatalf("join node = %q, want worker-1(LIFO 序号递增)", gotNode)
	}
	if srv.nodes.count() != 2 {
		t.Fatalf("nodes = %d, want 2", srv.nodes.count())
	}
	if srv.LastReadyLatency() <= 0 {
		t.Fatal("Ready 时延应被记录(原型=join RPC 完成)")
	}
	// 再扩:worker-2
	if err := srv.applyScale(context.Background(), DecideScaleOut); err != nil {
		t.Fatal(err)
	}
	if srv.nodes.count() != 3 {
		t.Fatalf("nodes = %d, want 3", srv.nodes.count())
	}
}

// 缩容接线:LIFO 选 victim → 摘出路由(DrainShardNode 时已不可路由)→
// DrainShardNode(推 L2 计划)→ RemoveShardNode 成功摘除。
func TestApplyScaleInDrainsAndRemoves(t *testing.T) {
	var drained, removed string
	var pickAtDrain string // drain 时刻的路由首节点(victim 应已摘出)
	var srv *Server
	srv = dialFakeCP(t, fakeCP{
		drainShardNode: func(_ context.Context, req *lakepb.DrainShardNodeRequest) (*lakepb.DrainShardNodeResponse, error) {
			drained = req.GetNodeId()
			pickAtDrain = srv.nodes.pick()
			return &lakepb.DrainShardNodeResponse{
				Ok: true, MigrationCount: 2,
				PushL2: []*lakepb.KVBlockID{{ModelId: "m", BlockHash: []byte("h")}},
			}, nil
		},
		removeShardNode: func(_ context.Context, req *lakepb.RemoveShardNodeRequest) (*lakepb.Ack, error) {
			removed = req.GetNodeId()
			return &lakepb.Ack{Ok: true}, nil
		},
	})
	defer srv.Close()
	srv.nodes = newNodeRegistry("worker-0", "worker-1")

	if err := srv.applyScale(context.Background(), DecideScaleIn); err != nil {
		t.Fatal(err)
	}
	if drained != "worker-1" || removed != "worker-1" {
		t.Fatalf("drained=%q removed=%q, want worker-1(LIFO victim)", drained, removed)
	}
	if pickAtDrain != "worker-0" {
		t.Fatalf("drain 时刻路由首节点 = %q, want worker-0(victim 已摘出,不丢新请求)", pickAtDrain)
	}
	if srv.nodes.count() != 1 || srv.nodes.pick() != "worker-0" {
		t.Fatalf("nodes = %d pick = %q, want 1/worker-0", srv.nodes.count(), srv.nodes.pick())
	}
}

// review #57 回归:Drain RPC 失败/被拒 → victim 回滚回 ready(容量不丢一档)。
func TestApplyScaleInDrainFailureRollsBack(t *testing.T) {
	for name, drainFn := range map[string]func(context.Context, *lakepb.DrainShardNodeRequest) (*lakepb.DrainShardNodeResponse, error){
		"rpc_error": func(context.Context, *lakepb.DrainShardNodeRequest) (*lakepb.DrainShardNodeResponse, error) {
			return nil, status.Error(codes.Unavailable, "cp down")
		},
		"rejected": func(context.Context, *lakepb.DrainShardNodeRequest) (*lakepb.DrainShardNodeResponse, error) {
			return &lakepb.DrainShardNodeResponse{Ok: false, Err: "unknown node"}, nil
		},
	} {
		t.Run(name, func(t *testing.T) {
			srv := dialFakeCP(t, fakeCP{drainShardNode: drainFn})
			defer srv.Close()
			srv.nodes = newNodeRegistry("worker-0", "worker-1")

			if err := srv.applyScale(context.Background(), DecideScaleIn); err == nil {
				t.Fatal("want error from failed drain")
			}
			if srv.nodes.count() != 2 {
				t.Fatalf("ready = %d, want 2(Drain 失败回滚,容量不丢)", srv.nodes.count())
			}
			if got := srv.nodes.drainingList(); len(got) != 0 {
				t.Fatalf("draining = %v, want [](回滚清 draining)", got)
			}
		})
	}
}

// review #57:pick 轮询分流(扩缩后请求分布到多节点);单节点退化为恒等。
func TestNodeRegistryPickRoundRobin(t *testing.T) {
	r := newNodeRegistry("worker-0", "worker-1")
	got := map[string]int{}
	for i := 0; i < 4; i++ {
		got[r.pick()]++
	}
	if got["worker-0"] != 2 || got["worker-1"] != 2 {
		t.Fatalf("pick 分布 = %v, want 各 2 次(轮询)", got)
	}
	single := newNodeRegistry("worker-0")
	for i := 0; i < 3; i++ {
		if single.pick() != "worker-0" {
			t.Fatal("单节点应恒等 worker-0")
		}
	}
}

// review #57:并发随 ready 节点数伸缩(扩容才真加执行容量)。
func TestSyncCapacityScalesWithNodes(t *testing.T) {
	srv := dialFakeCP(t, fakeCP{
		joinShardNode: func(context.Context, *lakepb.JoinShardNodeRequest) (*lakepb.JoinShardNodeResponse, error) {
			return &lakepb.JoinShardNodeResponse{Ok: true, Map: &lakepb.ShardMap{Generation: 2}}, nil
		},
	})
	defer srv.Close()
	srv.nodes = newNodeRegistry("worker-0")
	srv.cfg.MaxInFlight = 2
	srv.sched = NewPQScheduler(2, time.Second)

	if err := srv.applyScale(context.Background(), DecideScaleOut); err != nil {
		t.Fatal(err)
	}
	if srv.sched.maxInFlight != 4 {
		t.Fatalf("maxInFlight = %d, want 4(2/节点 × 2 节点)", srv.sched.maxInFlight)
	}
}

// 缩容完成门:placement 未清(RemoveShardNode 拒)→ 留 draining 不丢;
// 下周期 reap 成功后摘除。
func TestApplyScaleInRemoveRefusedStaysDraining(t *testing.T) {
	var removeCalls int
	srv := dialFakeCP(t, fakeCP{
		drainShardNode: func(_ context.Context, req *lakepb.DrainShardNodeRequest) (*lakepb.DrainShardNodeResponse, error) {
			return &lakepb.DrainShardNodeResponse{Ok: true}, nil
		},
		removeShardNode: func(_ context.Context, req *lakepb.RemoveShardNodeRequest) (*lakepb.Ack, error) {
			removeCalls++
			if removeCalls == 1 {
				return &lakepb.Ack{Ok: false, Err: "placements not cleared"}, nil
			}
			return &lakepb.Ack{Ok: true}, nil
		},
	})
	defer srv.Close()
	srv.nodes = newNodeRegistry("worker-0", "worker-1")

	if err := srv.applyScale(context.Background(), DecideScaleIn); err != nil {
		t.Fatal(err)
	}
	// 第一次 remove 被拒:节点留 draining(路由仍排除,不丢)
	if srv.nodes.count() != 1 {
		t.Fatalf("ready = %d, want 1(draining 不计入)", srv.nodes.count())
	}
	if got := srv.nodes.drainingList(); len(got) != 1 || got[0] != "worker-1" {
		t.Fatalf("draining = %v, want [worker-1]", got)
	}
	// 下周期 reap:placement 已清 → 摘除
	srv.reapDraining(context.Background())
	if got := srv.nodes.drainingList(); len(got) != 0 {
		t.Fatalf("draining = %v, want [](reap 成功)", got)
	}
	if removeCalls != 2 {
		t.Fatalf("remove calls = %d, want 2(拒后重试)", removeCalls)
	}
}

// tick 级集成:真实 PQScheduler 队列深度驱动决策 → CP join。
func TestAutoscaleTickDrivenBySchedulerQueue(t *testing.T) {
	var joins int
	var mu sync.Mutex
	srv := dialFakeCP(t, fakeCP{
		joinShardNode: func(_ context.Context, req *lakepb.JoinShardNodeRequest) (*lakepb.JoinShardNodeResponse, error) {
			mu.Lock()
			joins++
			mu.Unlock()
			return &lakepb.JoinShardNodeResponse{Ok: true, Map: &lakepb.ShardMap{Generation: 2}}, nil
		},
	})
	defer srv.Close()
	srv.nodes = newNodeRegistry("worker-0")
	srv.scaler = NewAutoscaler(AutoscaleConfig{
		MinNodes: 1, MaxNodes: 3,
		ScaleOutQueueLen: 2, SustainPeriods: 1, Cooldown: 0,
	})
	srv.sched = NewPQScheduler(1, time.Second)
	srv.cfg.Autoscale = true // tick 的扩缩段由开关门控(命中上报段不门控)
	schedCtx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go srv.sched.Run(schedCtx)

	// 1 在途(阻塞)+ 2 排队 → queue_len=2 达阈值
	release := make(chan struct{})
	defer close(release)
	block := func(ctx context.Context) error {
		select {
		case <-release:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	go func() { _ = srv.sched.Submit(context.Background(), 0, "m", block) }()
	go func() { _ = srv.sched.Submit(context.Background(), 0, "m", block) }()
	go func() { _ = srv.sched.Submit(context.Background(), 0, "m", block) }()
	waitQueueLen(t, srv.sched, 2)

	srv.autoscaleTick(context.Background())
	mu.Lock()
	defer mu.Unlock()
	if joins != 1 {
		t.Fatalf("joins = %d, want 1(队列深度驱动扩容)", joins)
	}
	if srv.nodes.count() != 2 {
		t.Fatalf("nodes = %d, want 2", srv.nodes.count())
	}
}

// 回归(bugbot #62):Autoscale 关(默认)时 tick 仍须上报命中——ReportHits 是
// B2 跟随流量放置/join warmup 的 Router 喂数源,不随扩缩容开关;且 scaler 为
// nil 也不得触碰(门控在评估段之前)。
func TestTickReportsHitsWhenAutoscaleOff(t *testing.T) {
	hitCh := make(chan string, 1)
	srv := dialFakeCP(t, fakeCP{
		reportHits: func(_ context.Context, req *lakepb.ReportHitsRequest) (*lakepb.Ack, error) {
			hitCh <- req.GetNodeId()
			return &lakepb.Ack{Ok: true}, nil
		},
	})
	defer srv.Close()
	srv.nodes = newNodeRegistry("worker-0")
	srv.hot = newHotSet(16)
	srv.scaler = nil // Autoscale 关时评估段不得执行
	srv.cfg.Autoscale = false

	tokens := []uint32{1, 2, 3, 4, 5, 6, 7, 8}
	hashes := ChainBlockHashes(tokens, BlockSize)
	if err := srv.Mirror().Apply(&lakepb.ViewUpdate{Seq: 0, Events: []*lakepb.ViewEvent{
		regEvent("m", string(hashes[0]), l0on("worker-0")),
	}}); err != nil {
		t.Fatal(err)
	}
	if hint := srv.prefixHint(context.Background(), "m", hashes, len(tokens), "worker-0"); hint.ReusedBlocks != 1 {
		t.Fatalf("hint = %+v, want 1 reused block", hint)
	}

	srv.autoscaleTick(context.Background()) // 不炸(scaler nil)+ 命中已上报
	select {
	case node := <-hitCh:
		if node != "worker-0" {
			t.Fatalf("ReportHits node = %q, want worker-0", node)
		}
	default:
		t.Fatal("Autoscale 关时 tick 未上报命中(ReportHits 断供)")
	}
}

// P7 收口判据(方案 Z):扩容后 Router **不再**指挥放置(无 PlaceBlocks);
// 命中观测经 autoscale tick 批量上报 CP(ReportHits),warmup 选块/发起归池侧。
func TestScaleOutReportsHitsNotPlacement(t *testing.T) {
	placeCalled := make(chan struct{}, 1)
	hitCh := make(chan []*lakepb.KVBlockID, 4)
	srv := dialFakeCP(t, fakeCP{
		joinShardNode: func(_ context.Context, req *lakepb.JoinShardNodeRequest) (*lakepb.JoinShardNodeResponse, error) {
			return &lakepb.JoinShardNodeResponse{Ok: true, Map: &lakepb.ShardMap{Generation: 2}}, nil
		},
		reportHits: func(_ context.Context, req *lakepb.ReportHitsRequest) (*lakepb.Ack, error) {
			hitCh <- req.GetIds()
			return &lakepb.Ack{Ok: true}, nil
		},
	})
	defer srv.Close()
	srv.nodes = newNodeRegistry("worker-0")
	srv.hot = newHotSet(16)
	srv.agent = fakeAgent{
		placeBlocks: func(_ context.Context, req *lakepb.PlaceBlocksRequest) (*lakepb.Ack, error) {
			placeCalled <- struct{}{}
			return &lakepb.Ack{Ok: true}, nil
		},
	}

	// 经 prefixHint 真实命中路径记录热块(镜像预置 L0 命中)
	tokens := []uint32{1, 2, 3, 4, 5, 6, 7, 8}
	hashes := ChainBlockHashes(tokens, BlockSize)
	if err := srv.Mirror().Apply(&lakepb.ViewUpdate{Seq: 0, Events: []*lakepb.ViewEvent{
		regEvent("m", string(hashes[0]), l0on("worker-0")),
	}}); err != nil {
		t.Fatal(err)
	}
	hint := srv.prefixHint(context.Background(), "m", hashes, len(tokens), "worker-0")
	if hint.ReusedBlocks != 1 {
		t.Fatalf("hint = %+v, want 1 reused block", hint)
	}

	if err := srv.applyScale(context.Background(), DecideScaleOut); err != nil {
		t.Fatal(err)
	}
	srv.flushHotHits(context.Background()) // autoscale tick 尾部的命中上报

	select {
	case ids := <-hitCh:
		if len(ids) != 1 || string(ids[0].GetBlockHash()) != string(hashes[0]) {
			t.Fatalf("reported ids = %d, want 1/%q(命中热块)", len(ids), hashes[0])
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timeout waiting for ReportHits")
	}
	select {
	case <-placeCalled:
		t.Fatal("Router 不得再指挥放置(PlaceBlocks 归池侧)")
	default:
	}
}
