package router

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"

	lakepb "github.com/chengda-wu/lake/go/pb"
)

// Config P3 Router 配置。
type Config struct {
	HTTPAddr   string // 默认 :8080
	WorkerAddr string // WorkerService,默认 127.0.0.1:50053
	AgentAddr  string // AgentService(边10 Dispatch),默认 127.0.0.1:50054
	CPAddr     string // ControlPlaneService(权威回退查询),默认 127.0.0.1:50051
}

// Server OpenAI 兼容 HTTP → Dispatch(agent) → Generate(worker)。
// P3 入口即本服务(边2);不经 Bifrost。
// P6.1:Router 已接 CP 客户端(LookupPrefix/Locate 权威回退档,冷启动/gap 用);
// 选路权威收归 Router(worker 不再自查前缀)归 P6.3。
type Server struct {
	cfg        Config
	worker     lakepb.WorkerServiceClient
	agent      lakepb.AgentServiceClient
	cp         lakepb.ControlPlaneServiceClient
	workerConn *grpc.ClientConn
	agentConn  *grpc.ClientConn
	cpConn     *grpc.ClientConn
}

const maxChatRequestBytes = 1 << 20

func New(cfg Config) (*Server, error) {
	if cfg.HTTPAddr == "" {
		cfg.HTTPAddr = ":8080"
	}
	if cfg.WorkerAddr == "" {
		cfg.WorkerAddr = "127.0.0.1:50053"
	}
	if cfg.AgentAddr == "" {
		cfg.AgentAddr = "127.0.0.1:50054"
	}
	if cfg.CPAddr == "" {
		cfg.CPAddr = "127.0.0.1:50051"
	}
	wconn, err := grpc.NewClient(cfg.WorkerAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, fmt.Errorf("dial worker: %w", err)
	}
	aconn, err := grpc.NewClient(cfg.AgentAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		_ = wconn.Close()
		return nil, fmt.Errorf("dial agent: %w", err)
	}
	cconn, err := grpc.NewClient(cfg.CPAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		_ = wconn.Close()
		_ = aconn.Close()
		return nil, fmt.Errorf("dial controlplane: %w", err)
	}
	return &Server{
		cfg:        cfg,
		worker:     lakepb.NewWorkerServiceClient(wconn),
		agent:      lakepb.NewAgentServiceClient(aconn),
		cp:         lakepb.NewControlPlaneServiceClient(cconn),
		workerConn: wconn,
		agentConn:  aconn,
		cpConn:     cconn,
	}, nil
}

func (s *Server) Close() error {
	var errs []error
	if s.workerConn != nil {
		errs = append(errs, s.workerConn.Close())
	}
	if s.agentConn != nil {
		errs = append(errs, s.agentConn.Close())
	}
	if s.cpConn != nil {
		errs = append(errs, s.cpConn.Close())
	}
	return errors.Join(errs...)
}

// LookupPrefixOnAuthority 冷路径权威查询(线性一致档):冷启动 / 镜像 gap 回退时
// 直查 CP 权威树。热路径(P6.2 起)读本地镜像,不走本方法。
func (s *Server) LookupPrefixOnAuthority(ctx context.Context, req *lakepb.LookupPrefixRequest) (*lakepb.LookupPrefixResponse, error) {
	if s.cp == nil {
		return nil, errors.New("controlplane client not configured")
	}
	return s.cp.LookupPrefix(ctx, req)
}

// LocateOnAuthority 与 LookupPrefix 同档的权威回退:按 block id 查位置。
func (s *Server) LocateOnAuthority(ctx context.Context, req *lakepb.LocateRequest) (*lakepb.LocateResponse, error) {
	if s.cp == nil {
		return nil, errors.New("controlplane client not configured")
	}
	return s.cp.Locate(ctx, req)
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/v1/chat/completions", s.handleChatCompletions)
	return mux
}

func (s *Server) ListenAndServe() error {
	log.Printf("lake-router OpenAI HTTP on %s → agent %s → worker %s",
		s.cfg.HTTPAddr, s.cfg.AgentAddr, s.cfg.WorkerAddr)
	return http.ListenAndServe(s.cfg.HTTPAddr, s.Handler())
}

type chatRequest struct {
	Model    string `json:"model"`
	Messages []struct {
		Role    string `json:"role"`
		Content string `json:"content"`
	} `json:"messages"`
	MaxTokens int `json:"max_tokens"`
}

type chatResponse struct {
	ID      string `json:"id"`
	Object  string `json:"object"`
	Created int64  `json:"created"`
	Model   string `json:"model"`
	Choices []struct {
		Index   int `json:"index"`
		Message struct {
			Role    string `json:"role"`
			Content string `json:"content"`
		} `json:"message"`
		FinishReason string `json:"finish_reason"`
	} `json:"choices"`
	Lake struct {
		ReusedBlocks  uint32 `json:"reused_blocks"`
		PrefillBlocks uint32 `json:"prefill_blocks"`
		Mode          string `json:"mode"`
	} `json:"lake"`
}

func (s *Server) handleChatCompletions(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxChatRequestBytes))
	if err != nil {
		http.Error(w, "request body too large", http.StatusRequestEntityTooLarge)
		return
	}
	var req chatRequest
	if err := json.Unmarshal(body, &req); err != nil {
		http.Error(w, "invalid JSON request", http.StatusBadRequest)
		return
	}
	promptText := ""
	for _, m := range req.Messages {
		if m.Role == "user" || m.Role == "system" {
			if promptText != "" {
				promptText += "\n"
			}
			promptText += m.Content
		}
	}
	tokens := tokenizeMock(promptText)
	maxNew := req.MaxTokens
	if maxNew <= 0 {
		maxNew = 4
	}
	model := req.Model
	if model == "" {
		model = "mock-llm"
	}
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()

	rid := uuid.NewString()
	nodeID := "worker-0"

	// 边10:先 Dispatch 到 agent(P3 仅 ack;执行仍在 Worker.Generate)。
	ack, err := s.agent.Dispatch(ctx, &lakepb.DispatchRequest{
		Mode:         "COLOCATED",
		TargetNodeId: nodeID,
		Hints:        map[string]string{"request_id": rid, "model_id": model},
	})
	if err != nil {
		code, msg := mapGRPCError("Dispatch", err)
		http.Error(w, msg, code)
		return
	}
	if !ack.GetOk() {
		if ack.GetErr() != "" {
			log.Printf("Dispatch rejected: %s", ack.GetErr())
		}
		http.Error(w, "Dispatch rejected", http.StatusBadGateway)
		return
	}

	gen, err := s.worker.Generate(ctx, &lakepb.GenerateRequest{
		RequestId:       rid,
		ModelId:         model,
		PromptTokens:    tokens,
		MaxNewTokens:    uint32(maxNew),
		RequesterNodeId: nodeID,
	})
	if err != nil {
		code, msg := mapGRPCError("Generate", err)
		http.Error(w, msg, code)
		return
	}

	content := detokenizeMock(gen.OutputTokens)
	resp := chatResponse{
		ID:      "chatcmpl-" + rid[:8],
		Object:  "chat.completion",
		Created: time.Now().Unix(),
		Model:   model,
	}
	resp.Choices = make([]struct {
		Index   int `json:"index"`
		Message struct {
			Role    string `json:"role"`
			Content string `json:"content"`
		} `json:"message"`
		FinishReason string `json:"finish_reason"`
	}, 1)
	resp.Choices[0].Index = 0
	resp.Choices[0].Message.Role = "assistant"
	resp.Choices[0].Message.Content = content
	resp.Choices[0].FinishReason = "stop"
	resp.Lake.ReusedBlocks = gen.ReusedBlocks
	resp.Lake.PrefillBlocks = gen.PrefillBlocks
	resp.Lake.Mode = gen.Mode

	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(resp)
}

func mapGRPCError(op string, err error) (httpStatus int, msg string) {
	st, ok := status.FromError(err)
	if !ok {
		log.Printf("%s RPC error: %v", op, err)
		return http.StatusBadGateway, op + " upstream failed"
	}
	log.Printf("%s RPC error: code=%s msg=%q", op, st.Code(), st.Message())
	switch st.Code() {
	case codes.InvalidArgument:
		return http.StatusBadRequest, op + " request invalid"
	case codes.Unavailable, codes.DeadlineExceeded:
		return http.StatusServiceUnavailable, op + " upstream unavailable"
	default:
		return http.StatusBadGateway, op + " upstream failed"
	}
}

// tokenizeMock:P3 跳过真实 tokenizer,按 rune 映射到稳定 uint32。
func tokenizeMock(s string) []uint32 {
	s = strings.TrimSpace(s)
	if s == "" {
		return []uint32{1, 2, 3, 4, 5, 6, 7, 8} // 至少一块
	}
	out := make([]uint32, 0, len(s))
	for _, r := range s {
		out = append(out, uint32(r)%10000+1)
	}
	// 补齐到 block 边界(8),稳定复用
	for len(out)%8 != 0 {
		out = append(out, 42)
	}
	return out
}

func detokenizeMock(tokens []uint32) string {
	parts := make([]string, len(tokens))
	for i, t := range tokens {
		parts[i] = fmt.Sprintf("%d", t)
	}
	return "mock:" + strings.Join(parts, ",")
}
