package router

import (
	"context"
	"net"
	"testing"
	"time"

	lakepb "github.com/chengda-wu/lake/go/pb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
)

// P6.4 判据(回路):Router 周期性上报 LoadReport;agent ack 携带池写背压 →
// 调度器暂停该 model 新启动(不丢请求),他模型不受影响。

// loadAgentSrv:ReportLoad 服务端假实现——收上报进 channel,每条 ack 可脚本化。
type loadAgentSrv struct {
	lakepb.UnimplementedAgentServiceServer
	received chan *lakepb.LoadReport
	ackFor   func() *lakepb.Ack
}

func (s *loadAgentSrv) ReportLoad(stream grpc.BidiStreamingServer[lakepb.LoadReport, lakepb.Ack]) error {
	for {
		rep, err := stream.Recv()
		if err != nil {
			return err
		}
		select {
		case s.received <- rep:
		default:
		}
		if err := stream.Send(s.ackFor()); err != nil {
			return err
		}
	}
}

func dialLoadAgent(t *testing.T, srv *loadAgentSrv) lakepb.AgentServiceClient {
	t.Helper()
	lis := bufconn.Listen(1 << 20)
	gsrv := grpc.NewServer()
	lakepb.RegisterAgentServiceServer(gsrv, srv)
	go func() { _ = gsrv.Serve(lis) }()
	t.Cleanup(gsrv.Stop)
	conn, err := grpc.NewClient(
		"passthrough:///bufconn",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return lakepb.NewAgentServiceClient(conn)
}

// backpressureActive 测试辅助:model 当前是否在背压窗口内。
func (s *PQScheduler) backpressureActive(model string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	dl, ok := s.bp[model]
	return ok && time.Now().Before(dl)
}

func waitBPActive(t *testing.T, s *PQScheduler, model string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if s.backpressureActive(model) {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timeout waiting for backpressure on %s", model)
}

func TestRunLoadSyncUploadsAndAppliesBackpressure(t *testing.T) {
	bpActive := make(chan struct{}, 1)
	srv := &loadAgentSrv{
		received: make(chan *lakepb.LoadReport, 16),
		ackFor: func() *lakepb.Ack {
			select {
			case <-bpActive:
				return &lakepb.Ack{Ok: true, Backpressure: &lakepb.BackpressureSignal{
					ModelId: "m-bp", Reason: "HARD_QUOTA", DeficitBytes: 64,
				}}
			default:
				return &lakepb.Ack{Ok: true}
			}
		},
	}
	client := dialLoadAgent(t, srv)
	sched := NewPQScheduler(1, 5*time.Second)
	schedCtx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go sched.Run(schedCtx)
	go RunLoadSync(schedCtx, client, sched, "router", time.Millisecond)

	// 1) Router 周期性上报 LoadSnapshot
	select {
	case rep := <-srv.received:
		if rep.GetNodeId() != "router" {
			t.Fatalf("node_id = %q, want router", rep.GetNodeId())
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timeout waiting for LoadReport upload")
	}

	// 2) agent ack 带上背压 → 等调度器确认生效(无竞态)
	bpActive <- struct{}{}
	waitBPActive(t, sched, "m-bp")

	probe := &pqProbe{}
	bpCtx, bpCancel := context.WithCancel(context.Background())
	t.Cleanup(bpCancel)
	bpDone := make(chan error, 1)
	go func() {
		bpDone <- sched.Submit(bpCtx, 5, "m-bp", func(context.Context) error {
			probe.record("bp")
			return nil
		})
	}()
	otherDone := submitInBackground(sched, 5, "other", func(context.Context) error {
		probe.record("other")
		return nil
	})
	if err := <-otherDone; err != nil {
		t.Fatalf("other err = %v", err)
	}
	select {
	case <-bpDone:
		t.Fatal("背压中的 m-bp 不应启动")
	case <-time.After(30 * time.Millisecond):
	}
	if got := probe.order(); len(got) != 1 || got[0] != "other" {
		t.Fatalf("order = %v, want [other](背压 model 推迟)", got)
	}

	// 3) 背压信号过期 → 自动恢复(不丢请求)
	sched2 := NewPQScheduler(1, time.Millisecond)
	sched2Ctx, cancel2 := context.WithCancel(context.Background())
	t.Cleanup(cancel2)
	go sched2.Run(sched2Ctx)
	sched2.SetBackpressure("m-bp", time.Now().Add(-time.Second)) // 已过期
	done2 := submitInBackground(sched2, 5, "m-bp", func(context.Context) error { return nil })
	if err := <-done2; err != nil {
		t.Fatalf("expired bp should not block: %v", err)
	}
}
