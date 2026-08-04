package router

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	lakepb "github.com/chengda-wu/lake/go/pb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

type fakeAgent struct {
	dispatch func(context.Context, *lakepb.DispatchRequest) (*lakepb.Ack, error)
}

func (f fakeAgent) Dispatch(ctx context.Context, req *lakepb.DispatchRequest, _ ...grpc.CallOption) (*lakepb.Ack, error) {
	return f.dispatch(ctx, req)
}

func (fakeAgent) ReportLoad(context.Context, ...grpc.CallOption) (grpc.BidiStreamingClient[lakepb.LoadReport, lakepb.Ack], error) {
	return nil, status.Error(codes.Unimplemented, "not implemented")
}

func (fakeAgent) PlaceBlocks(context.Context, *lakepb.PlaceBlocksRequest, ...grpc.CallOption) (*lakepb.Ack, error) {
	return nil, status.Error(codes.Unimplemented, "not implemented")
}

type fakeWorker struct {
	generate func(context.Context, *lakepb.GenerateRequest) (*lakepb.GenerateResponse, error)
}

func (f fakeWorker) Generate(ctx context.Context, req *lakepb.GenerateRequest, _ ...grpc.CallOption) (*lakepb.GenerateResponse, error) {
	return f.generate(ctx, req)
}

// fakeCP:P6.1 用进程内 gRPC 服务冒充 CP 权威树(嵌入 Unimplemented 兜底其余 RPC)。
type fakeCP struct {
	lakepb.UnimplementedControlPlaneServiceServer
	lookupPrefix  func(context.Context, *lakepb.LookupPrefixRequest) (*lakepb.LookupPrefixResponse, error)
	locate        func(context.Context, *lakepb.LocateRequest) (*lakepb.LocateResponse, error)
	subscribeView func(*lakepb.SubscribeRequest, grpc.ServerStreamingServer[lakepb.ViewUpdate]) error
}

func (f fakeCP) LookupPrefix(ctx context.Context, req *lakepb.LookupPrefixRequest) (*lakepb.LookupPrefixResponse, error) {
	return f.lookupPrefix(ctx, req)
}

func (f fakeCP) Locate(ctx context.Context, req *lakepb.LocateRequest) (*lakepb.LocateResponse, error) {
	return f.locate(ctx, req)
}

func (f fakeCP) SubscribeView(
	req *lakepb.SubscribeRequest,
	stream grpc.ServerStreamingServer[lakepb.ViewUpdate],
) error {
	if f.subscribeView == nil {
		return status.Error(codes.Unimplemented, "not implemented")
	}
	return f.subscribeView(req, stream)
}

// dialFakeCP 起 bufconn 进程内 CP,返回接好客户端的 Server。
func dialFakeCP(t *testing.T, cp lakepb.ControlPlaneServiceServer) *Server {
	t.Helper()
	lis := bufconn.Listen(1 << 20)
	gsrv := grpc.NewServer()
	lakepb.RegisterControlPlaneServiceServer(gsrv, cp)
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
	t.Cleanup(func() { _ = conn.Close() })
	s := &Server{cp: lakepb.NewControlPlaneServiceClient(conn), cpConn: conn, mirror: NewViewMirror()}
	ctx, cancel := context.WithCancel(context.Background())
	s.viewCancel = cancel
	t.Cleanup(cancel)
	go RunViewSync(ctx, s.cp, s.mirror, "router-test", time.Millisecond)
	return s
}

// P6.1 判据:Go Router 单测能查 CP 权威树(经真实 gRPC 编解码链路)。
func TestLookupPrefixOnAuthorityQueriesCP(t *testing.T) {
	srv := dialFakeCP(t, fakeCP{
		lookupPrefix: func(_ context.Context, req *lakepb.LookupPrefixRequest) (*lakepb.LookupPrefixResponse, error) {
			if req.GetModelId() != "m" || req.GetRequesterNodeId() != "n0" {
				return nil, status.Error(codes.InvalidArgument, "bad request")
			}
			return &lakepb.LookupPrefixResponse{
				Blocks: []*lakepb.ReusableBlock{{
					Id:       &lakepb.KVBlockID{ModelId: "m", BlockHash: []byte("h0")},
					LocalHit: true,
				}},
				HitLength:   1,
				AllLocalHit: true,
			}, nil
		},
	})

	resp, err := srv.LookupPrefixOnAuthority(context.Background(), &lakepb.LookupPrefixRequest{
		ModelId:         "m",
		PoolKind:        lakepb.PoolKind_TARGET,
		PrefixHashes:    [][]byte{[]byte("h0")},
		RequesterNodeId: "n0",
	})
	if err != nil {
		t.Fatal(err)
	}
	if resp.GetHitLength() != 1 || !resp.GetAllLocalHit() {
		t.Fatalf("resp = %+v, want hit_length=1 all_local_hit=true", resp)
	}
	if !resp.GetBlocks()[0].GetLocalHit() {
		t.Fatalf("block local_hit = false, want true (D-direct 信号)")
	}
}

func TestLocateOnAuthorityQueriesCP(t *testing.T) {
	srv := dialFakeCP(t, fakeCP{
		locate: func(_ context.Context, req *lakepb.LocateRequest) (*lakepb.LocateResponse, error) {
			if len(req.GetIds()) != 1 {
				return nil, status.Error(codes.InvalidArgument, "want 1 id")
			}
			return &lakepb.LocateResponse{
				Blocks: []*lakepb.BlockMeta{{
					Id:        req.GetIds()[0],
					L3Present: true,
				}},
			}, nil
		},
	})

	resp, err := srv.LocateOnAuthority(context.Background(), &lakepb.LocateRequest{
		Ids: []*lakepb.KVBlockID{{ModelId: "m", BlockHash: []byte("h0")}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(resp.GetBlocks()) != 1 || !resp.GetBlocks()[0].GetL3Present() {
		t.Fatalf("resp = %+v, want 1 block with l3_present", resp)
	}
}

func TestAuthorityQueryWithoutCPClientFails(t *testing.T) {
	srv := &Server{}
	if _, err := srv.LookupPrefixOnAuthority(context.Background(), &lakepb.LookupPrefixRequest{}); err == nil {
		t.Fatal("want error when cp client not configured")
	}
	if _, err := srv.LocateOnAuthority(context.Background(), &lakepb.LocateRequest{}); err == nil {
		t.Fatal("want error when cp client not configured")
	}
}

// P6.2 判据:Router 后台 view sync 建镜像后,选路读本地镜像零 RPC。
func TestServerViewSyncBuildsMirror(t *testing.T) {
	srv := dialFakeCP(t, fakeCP{
		subscribeView: func(req *lakepb.SubscribeRequest, stream grpc.ServerStreamingServer[lakepb.ViewUpdate]) error {
			if req.GetResumeFromSeq() != 0 {
				return status.Error(codes.FailedPrecondition, "want cold subscribe")
			}
			for _, u := range []*lakepb.ViewUpdate{
				{Seq: 0, Events: []*lakepb.ViewEvent{regEvent("m", "h0", l0on("n0")), regEvent("m", "h1")}},
				{Seq: 1}, // 锚点
			} {
				if err := stream.Send(u); err != nil {
					return err
				}
			}
			<-stream.Context().Done()
			return nil
		},
	})
	defer srv.Close()
	waitFor(t, 3*time.Second, "mirror synced to seq 1", func() bool {
		return srv.Mirror().LastSeq() == 1
	})
	blocks, hitLen, allLocal := srv.Mirror().PrefixLookup(
		"m", "", lakepb.PoolKind_TARGET, [][]byte{[]byte("h0"), []byte("h1")}, "n0")
	if hitLen != 2 {
		t.Fatalf("hitLen = %d, want 2", hitLen)
	}
	if allLocal {
		t.Fatal("allLocal = true, want false(h1 不在 n0)")
	}
	if !blocks[0].LocalHit || blocks[1].LocalHit {
		t.Fatalf("local_hit = [%v %v], want [true false]", blocks[0].LocalHit, blocks[1].LocalHit)
	}
}

func testServer(t *testing.T, agent lakepb.AgentServiceClient, worker lakepb.WorkerServiceClient) *Server {
	// P6.3:handler 选路读镜像;cp=nil 时 prefixHint 权威回退报错按冷请求处理。
	// P6.4:handler 经 PQScheduler 执行;测试用大并发(不引入排队/抢占时序),
	// 调度语义由 pq_test.go 专测。
	t.Helper()
	s := &Server{
		agent:  agent,
		worker: worker,
		mirror: NewViewMirror(),
		cfg:    Config{NodeRole: string(RoleHybrid), MaxInFlight: 64},
		sched:  NewPQScheduler(64, time.Second),
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go s.sched.Run(ctx)
	return s
}

func postChat(t *testing.T, srv *Server, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	return rec
}

func TestChatBodyLimit(t *testing.T) {
	srv := testServer(t, nil, nil)
	rec := postChat(t, srv, strings.Repeat("x", maxChatRequestBytes+1))
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status=%d body=%q", rec.Code, rec.Body.String())
	}
}

func TestServerCloseClosesClientConnections(t *testing.T) {
	workerConn, err := grpc.NewClient("passthrough:///worker", grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	agentConn, err := grpc.NewClient("passthrough:///agent", grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	srv := &Server{workerConn: workerConn, agentConn: agentConn}

	if err := srv.Close(); err != nil {
		t.Fatalf("Close() error = %v", err)
	}
	if state := workerConn.GetState().String(); state != "SHUTDOWN" {
		t.Fatalf("workerConn state = %s, want SHUTDOWN", state)
	}
	if state := agentConn.GetState().String(); state != "SHUTDOWN" {
		t.Fatalf("agentConn state = %s, want SHUTDOWN", state)
	}
}

func TestInvalidJSONUsesGenericError(t *testing.T) {
	srv := testServer(t, nil, nil)
	rec := postChat(t, srv, "{")
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status=%d body=%q", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	if !strings.Contains(body, "invalid JSON request") || strings.Contains(body, "unexpected end") {
		t.Fatalf("unexpected body: %q", body)
	}
}

func TestGRPCErrorDoesNotLeakBackendMessage(t *testing.T) {
	srv := testServer(t, fakeAgent{
		dispatch: func(context.Context, *lakepb.DispatchRequest) (*lakepb.Ack, error) {
			return nil, status.Error(codes.Unavailable, "secret backend details")
		},
	}, fakeWorker{})
	rec := postChat(t, srv, `{"model":"m","messages":[{"role":"user","content":"hi"}]}`)
	body, _ := io.ReadAll(rec.Result().Body)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status=%d body=%q", rec.Code, string(body))
	}
	if strings.Contains(string(body), "secret backend details") {
		t.Fatalf("leaked backend details: %q", string(body))
	}
}

func TestRejectedAckDoesNotLeakReason(t *testing.T) {
	srv := testServer(t, fakeAgent{
		dispatch: func(context.Context, *lakepb.DispatchRequest) (*lakepb.Ack, error) {
			return &lakepb.Ack{Ok: false, Err: "secret quota details"}, nil
		},
	}, fakeWorker{})
	rec := postChat(t, srv, `{"model":"m","messages":[{"role":"user","content":"hi"}]}`)
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status=%d body=%q", rec.Code, rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), "secret quota details") {
		t.Fatalf("leaked ack err: %q", rec.Body.String())
	}
}

// P6.3 判据:Router 读镜像选路并下发——本地命中 → D-direct,GenerateRequest 带 mode/hint。
func TestChatSelectsDDirectFromMirror(t *testing.T) {
	var gotDispatch *lakepb.DispatchRequest
	var gotGen *lakepb.GenerateRequest
	srv := testServer(t, fakeAgent{
		dispatch: func(_ context.Context, req *lakepb.DispatchRequest) (*lakepb.Ack, error) {
			gotDispatch = req
			return &lakepb.Ack{Ok: true}, nil
		},
	}, fakeWorker{
		generate: func(_ context.Context, req *lakepb.GenerateRequest) (*lakepb.GenerateResponse, error) {
			gotGen = req
			return &lakepb.GenerateResponse{RequestId: req.GetRequestId(), Mode: req.GetExecMode()}, nil
		},
	})
	// 预置镜像:该 prompt 的首块在 worker-0 L0(本地命中,部分命中也算 D-direct)
	tokens := tokenizeMock("hi")
	hashes := ChainBlockHashes(tokens, BlockSize)
	if err := srv.mirror.Apply(&lakepb.ViewUpdate{Seq: 0, Events: []*lakepb.ViewEvent{
		regEvent("m", string(hashes[0]), l0on("worker-0")),
	}}); err != nil {
		t.Fatal(err)
	}
	rec := postChat(t, srv, `{"model":"m","messages":[{"role":"user","content":"hi"}]}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%q", rec.Code, rec.Body.String())
	}
	if gotDispatch.GetMode() != string(ExecModeDDirect) {
		t.Fatalf("Dispatch.Mode = %q, want D_DIRECT", gotDispatch.GetMode())
	}
	if gotGen.GetExecMode() != string(ExecModeDDirect) || !gotGen.GetLocalHit() ||
		gotGen.GetReusedBlocks() != 1 || gotGen.GetComputedTokens() != 8 {
		t.Fatalf("GenerateRequest = %+v, want D_DIRECT local reused=1 computed=8", gotGen)
	}
}

// 镜像无命中且权威不可达(cp=nil)→ 冷请求 COLOCATED 无复用,不阻塞执行。
func TestChatColdMissColocated(t *testing.T) {
	var gotGen *lakepb.GenerateRequest
	srv := testServer(t, fakeAgent{
		dispatch: func(context.Context, *lakepb.DispatchRequest) (*lakepb.Ack, error) {
			return &lakepb.Ack{Ok: true}, nil
		},
	}, fakeWorker{
		generate: func(_ context.Context, req *lakepb.GenerateRequest) (*lakepb.GenerateResponse, error) {
			gotGen = req
			return &lakepb.GenerateResponse{RequestId: req.GetRequestId(), Mode: req.GetExecMode()}, nil
		},
	})
	rec := postChat(t, srv, `{"model":"m","messages":[{"role":"user","content":"hi"}]}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%q", rec.Code, rec.Body.String())
	}
	if gotGen.GetExecMode() != string(ExecModeColocated) || gotGen.GetReusedBlocks() != 0 ||
		gotGen.GetComputedTokens() != 0 || gotGen.GetLocalHit() {
		t.Fatalf("GenerateRequest = %+v, want COLOCATED 无复用", gotGen)
	}
}
