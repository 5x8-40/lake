package router

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	lakepb "github.com/chengda-wu/lake/go/pb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
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

func testServer(agent lakepb.AgentServiceClient, worker lakepb.WorkerServiceClient) *Server {
	return &Server{agent: agent, worker: worker}
}

func postChat(t *testing.T, srv *Server, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	return rec
}

func TestChatBodyLimit(t *testing.T) {
	srv := testServer(nil, nil)
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
	srv := testServer(nil, nil)
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
	srv := testServer(fakeAgent{
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
	srv := testServer(fakeAgent{
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
