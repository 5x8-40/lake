package router

import (
	"context"
	"net"
	"sync"
	"testing"
	"time"

	lakepb "github.com/chengda-wu/lake/go/pb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

// scriptCP:按脚本应答 SubscribeView 的假 CP,记录每次调用的 resume_from_seq。
type scriptCP struct {
	lakepb.UnimplementedControlPlaneServiceServer
	mu    sync.Mutex
	calls []uint64
	serve func(call int, stream grpc.ServerStreamingServer[lakepb.ViewUpdate]) error
}

func (s *scriptCP) SubscribeView(
	req *lakepb.SubscribeRequest,
	stream grpc.ServerStreamingServer[lakepb.ViewUpdate],
) error {
	s.mu.Lock()
	call := len(s.calls)
	s.calls = append(s.calls, req.GetResumeFromSeq())
	s.mu.Unlock()
	return s.serve(call, stream)
}

func (s *scriptCP) resumeSeqs() []uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]uint64(nil), s.calls...)
}

func dialScriptCP(t *testing.T, cp *scriptCP) lakepb.ControlPlaneServiceClient {
	t.Helper()
	lis := bufconn.Listen(1 << 20)
	srv := grpc.NewServer()
	lakepb.RegisterControlPlaneServiceServer(srv, cp)
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)
	conn, err := grpc.NewClient(
		"passthrough:///bufconn",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
	)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() { conn.Close() })
	return lakepb.NewControlPlaneServiceClient(conn)
}

func anchor(seq uint64) *lakepb.ViewUpdate { return &lakepb.ViewUpdate{Seq: seq} }

// blockUntilCancel 发完脚本后保持流打开(模拟常驻订阅),直到客户端取消。
func blockUntilCancel(stream grpc.ServerStreamingServer[lakepb.ViewUpdate]) error {
	<-stream.Context().Done()
	return nil
}

func waitFor(t *testing.T, d time.Duration, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timeout waiting for %s", what)
}

// 判据:CP 推变更 → 镜像最终一致;断线 gap → resume 重连 → 镜像与权威对齐。
func TestRunViewSyncGapThenResume(t *testing.T) {
	cp := &scriptCP{}
	cp.serve = func(call int, stream grpc.ServerStreamingServer[lakepb.ViewUpdate]) error {
		var ups []*lakepb.ViewUpdate
		switch call {
		case 0: // 冷启动:快照 + 锚点(1),随后发来 seq=3(跳过 2 → 客户端 gap)
			ups = []*lakepb.ViewUpdate{
				{Seq: 0, Events: []*lakepb.ViewEvent{regEvent("m", "h0")}},
				anchor(1),
				{Seq: 3, Events: []*lakepb.ViewEvent{regEvent("m", "h1")}},
			}
		case 1: // resume=1:重放 2,3 → 对齐
			ups = []*lakepb.ViewUpdate{
				{Seq: 2, Events: []*lakepb.ViewEvent{{
					Kind: lakepb.ViewEvent_MOVED,
					Id: &lakepb.KVBlockID{
						ModelId: "m", PoolKind: lakepb.PoolKind_TARGET, BlockHash: []byte("h0"),
					},
					Locations: []*lakepb.Location{l0on("n0")},
				}}},
				{Seq: 3, Events: []*lakepb.ViewEvent{regEvent("m", "h1")}},
			}
		default:
			return status.Errorf(codes.Internal, "unexpected call %d", call)
		}
		for _, u := range ups {
			if err := stream.Send(u); err != nil {
				return err
			}
		}
		if call == 0 {
			return status.Error(codes.Unavailable, "drop")
		}
		return blockUntilCancel(stream)
	}
	client := dialScriptCP(t, cp)
	mirror := NewViewMirror()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		_ = RunViewSync(ctx, client, mirror, "r0", time.Millisecond)
		close(done)
	}()
	waitFor(t, 3*time.Second, "mirror aligned at seq 3", func() bool {
		return mirror.LastSeq() == 3
	})
	cancel()
	<-done
	if got := cp.resumeSeqs(); len(got) != 2 || got[0] != 0 || got[1] != 1 {
		t.Fatalf("resume seqs = %v, want [0 1]", got)
	}
	blk, ok := mirror.Locate(&lakepb.KVBlockID{
		ModelId: "m", PoolKind: lakepb.PoolKind_TARGET, BlockHash: []byte("h0"),
	}, "n0")
	if !ok || !blk.LocalHit {
		t.Fatalf("h0 after replay: ok=%v local_hit=%v, want true/true", ok, blk.LocalHit)
	}
	if _, ok := mirror.Locate(&lakepb.KVBlockID{
		ModelId: "m", PoolKind: lakepb.PoolKind_TARGET, BlockHash: []byte("h1"),
	}, ""); !ok {
		t.Fatal("h1 missing after gap resume")
	}
}

// 判据:resume 点过老(replay buffer 已逐出)→ CP 回退快照,镜像重置对齐。
func TestRunViewSyncBufferOverflowSnapshotFallback(t *testing.T) {
	cp := &scriptCP{}
	cp.serve = func(call int, stream grpc.ServerStreamingServer[lakepb.ViewUpdate]) error {
		var ups []*lakepb.ViewUpdate
		switch call {
		case 0: // 冷启动:快照含 h0,h1 + 锚点(1),随后断线
			ups = []*lakepb.ViewUpdate{
				{Seq: 0, Events: []*lakepb.ViewEvent{regEvent("m", "h0"), regEvent("m", "h1")}},
				anchor(1),
			}
		case 1: // resume=1 但 CP buffer 已逐出 → 回退快照(只剩 h1)+ 锚点(5)
			ups = []*lakepb.ViewUpdate{
				{Seq: 0, Events: []*lakepb.ViewEvent{regEvent("m", "h1")}},
				anchor(5),
			}
		default:
			return status.Errorf(codes.Internal, "unexpected call %d", call)
		}
		for _, u := range ups {
			if err := stream.Send(u); err != nil {
				return err
			}
		}
		if call == 0 {
			return status.Error(codes.Unavailable, "drop")
		}
		return blockUntilCancel(stream)
	}
	client := dialScriptCP(t, cp)
	mirror := NewViewMirror()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		_ = RunViewSync(ctx, client, mirror, "r0", time.Millisecond)
		close(done)
	}()
	waitFor(t, 3*time.Second, "mirror reset by snapshot fallback", func() bool {
		return mirror.LastSeq() == 5
	})
	cancel()
	<-done
	if got := cp.resumeSeqs(); len(got) != 2 || got[0] != 0 || got[1] != 1 {
		t.Fatalf("resume seqs = %v, want [0 1]", got)
	}
	if _, ok := mirror.Locate(&lakepb.KVBlockID{
		ModelId: "m", PoolKind: lakepb.PoolKind_TARGET, BlockHash: []byte("h0"),
	}, ""); ok {
		t.Fatal("h0 must be reset away by snapshot fallback")
	}
	if _, ok := mirror.Locate(&lakepb.KVBlockID{
		ModelId: "m", PoolKind: lakepb.PoolKind_TARGET, BlockHash: []byte("h1"),
	}, ""); !ok {
		t.Fatal("h1 missing after snapshot fallback")
	}
}
