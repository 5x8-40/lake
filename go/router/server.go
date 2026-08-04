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
	"sync"
	"sync/atomic"
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
	HTTPAddr    string // 默认 :8080
	WorkerAddr  string // WorkerService,默认 127.0.0.1:50053
	AgentAddr   string // AgentService(边10 Dispatch),默认 127.0.0.1:50054
	CPAddr      string // ControlPlaneService(权威回退查询),默认 127.0.0.1:50051
	NodeRole    string // 候选执行节点角色(hybrid/prefill/decode,LAKE_NODE_ROLE),P6.3 选路输入;单节点原型默认 hybrid
	MaxInFlight int    // P6.4:单节点并发执行上限(P6.5 起总并发=×ready 节点数;非准入控制——队列无界不拒请求);单 worker mock 默认 1
	Autoscale   bool   // P6.5:基于指标的弹性扩缩(LAKE_AUTOSCALE=1);默认关——单进程原型不起真实 worker
}

// Server OpenAI 兼容 HTTP → Dispatch(agent) → Generate(worker)。
// P3 入口即本服务(边2);不经 Bifrost。
// P6.1:Router 已接 CP 客户端(LookupPrefix/Locate 权威回退档,冷启动/gap 用)。
// P6.2:后台 RunViewSync 消费 CP SubscribeView 维护本地只读镜像(ViewMirror);
// 选路读镜像零 RPC,gap/断线自动 resume,resume 过老 CP 回退快照。
// P6.3:选路权威收归 Router——chainBlockHashes + 镜像 PrefixLookup(miss 回退权威)
// → SelectExecMode → GenerateRequest.exec_mode/hint 下发;worker 不再自查前缀。
// P6.4:已准入请求经 PQScheduler 按 priority 排序/抢占(被抢占者重排不丢,KV 留池
// 重跑命中前缀);RunLoadSync 上报 LoadSnapshot 并回收 agent 转发来的池写背压
// (触硬配额 → 该 model 新启动暂停,池间流控;shedding 归 gateway,Router 不拒请求)。
// P6.5:可选弹性扩缩(LAKE_AUTOSCALE=1)——按队列深度等指标决策,扩容 JoinShardNode
// (一致性哈希最小迁移)入路由表,缩容 DrainShardNode(推 L2 计划)摘路由、
// placement 清后 RemoveShardNode;真实 provision 归外部编排,Ready<10s 待 P7。
type Server struct {
	cfg         Config
	worker      lakepb.WorkerServiceClient
	agent       lakepb.AgentServiceClient
	cp          lakepb.ControlPlaneServiceClient
	workerConn  *grpc.ClientConn
	agentConn   *grpc.ClientConn
	cpConn      *grpc.ClientConn
	mirror      *ViewMirror
	viewCancel  context.CancelFunc
	sched       *PQScheduler
	schedCancel context.CancelFunc

	// P6.5:扩缩容状态
	nodes                  *nodeRegistry
	scaler                 *Autoscaler
	lastReadyLatencyMu     sync.Mutex
	lastReadyLatency       time.Duration // 最近扩容决策→Ready 时延(原型=join RPC 完成)
	hitBlocks, totalBlocks atomic.Uint64 // 命中率分子/分母(累计 block 数)
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
	if cfg.NodeRole == "" {
		cfg.NodeRole = string(RoleHybrid)
	}
	if cfg.MaxInFlight <= 0 {
		cfg.MaxInFlight = 1
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
	s := &Server{
		cfg:        cfg,
		worker:     lakepb.NewWorkerServiceClient(wconn),
		agent:      lakepb.NewAgentServiceClient(aconn),
		cp:         lakepb.NewControlPlaneServiceClient(cconn),
		workerConn: wconn,
		agentConn:  aconn,
		cpConn:     cconn,
		mirror:     NewViewMirror(),
	}
	ctx, cancel := context.WithCancel(context.Background())
	s.viewCancel = cancel
	go RunViewSync(ctx, s.cp, s.mirror, "router", 200*time.Millisecond)
	s.sched = NewPQScheduler(cfg.MaxInFlight, 30*time.Second)
	schedCtx, schedCancel := context.WithCancel(context.Background())
	s.schedCancel = schedCancel
	go s.sched.Run(schedCtx)
	go RunLoadSync(schedCtx, s.agent, s.sched, "router", time.Second)
	s.nodes = newNodeRegistry("worker-0")
	s.scaler = NewAutoscaler(AutoscaleConfig{})
	if cfg.Autoscale {
		go s.runAutoscale(schedCtx, 2*time.Second)
	}
	return s, nil
}

func (s *Server) Close() error {
	if s.viewCancel != nil {
		s.viewCancel()
	}
	if s.schedCancel != nil {
		s.schedCancel()
	}
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

// Mirror 本地只读位置视图镜像(P6.2;选路零 RPC 的读取入口)。
func (s *Server) Mirror() *ViewMirror {
	return s.mirror
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
	Priority  int `json:"priority"` // P6.4:大者优先;缺省 0。仅决定已准入请求顺序/抢占,不丢请求
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
	nodeID := s.pickNode()

	// P6.3:选路权威在 Router——哈希链 → 镜像查命中(miss 回退权威)→ 选模式。
	// 纯内存读,模式选择开销 µs 级,满足 D-direct < 5ms 的 SLO 预算。
	hashes := ChainBlockHashes(tokens, BlockSize)
	hint := s.prefixHint(ctx, model, hashes, len(tokens), nodeID)
	mode := SelectExecMode(hint, len(tokens), WorkerRole(s.cfg.NodeRole))

	// P6.4:已准入请求进优先级调度器(排序/抢占/背压暂停;不丢请求)。
	// 抢占重排会重跑本闭包——attempt 后缀避免 worker 侧 duplicate req_id;
	// 被抢占者 KV 已留池,重跑命中前缀(抢占重算式,对齐 vLLM v1)。
	var gen *lakepb.GenerateResponse
	attempt := 0
	exec := func(execCtx context.Context) error {
		attempt++
		reqID := rid
		if attempt > 1 {
			reqID = fmt.Sprintf("%s-r%d", rid, attempt)
		}
		// 边10:先 Dispatch 到 agent(P3 仅 ack;执行仍在 Worker.Generate)。
		ack, err := s.agent.Dispatch(execCtx, &lakepb.DispatchRequest{
			Mode:         string(mode),
			TargetNodeId: nodeID,
			Hints:        map[string]string{"request_id": reqID, "model_id": model},
		})
		if err != nil {
			return &dispatchCallError{err: err}
		}
		if !ack.GetOk() {
			if ack.GetErr() != "" {
				log.Printf("Dispatch rejected: %s", ack.GetErr())
			}
			return errDispatchRejected
		}
		g, err := s.worker.Generate(execCtx, &lakepb.GenerateRequest{
			RequestId:       reqID,
			ModelId:         model,
			PromptTokens:    tokens,
			MaxNewTokens:    uint32(maxNew),
			RequesterNodeId: nodeID,
			ExecMode:        string(mode),
			ComputedTokens:  uint32(hint.ComputedTokens),
			ReusedBlocks:    uint32(hint.ReusedBlocks),
			LocalHit:        hint.LocalHit,
		})
		if err != nil {
			return &generateCallError{err: err}
		}
		gen = g
		return nil
	}
	if err := s.sched.Submit(ctx, req.Priority, model, exec); err != nil {
		var dce *dispatchCallError
		var gce *generateCallError
		switch {
		case errors.As(err, &dce):
			code, msg := mapGRPCError("Dispatch", dce.err)
			http.Error(w, msg, code)
		case errors.Is(err, errDispatchRejected):
			http.Error(w, "Dispatch rejected", http.StatusBadGateway)
		case errors.As(err, &gce):
			code, msg := mapGRPCError("Generate", gce.err)
			http.Error(w, msg, code)
		default:
			// 客户端断开/超时(ctx)或调度器内部错误
			code, msg := mapGRPCError("Generate", err)
			http.Error(w, msg, code)
		}
		return
	}

	// P6.5:命中率统计(扩缩决策输入之一;累计口径,原型足够)
	s.totalBlocks.Add(uint64(len(hashes)))
	s.hitBlocks.Add(uint64(gen.GetReusedBlocks()))

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

// pickNode 选目标执行节点(单节点原型 = worker-0;P6.5 扩缩后读路由表首节点)。
func (s *Server) pickNode() string {
	if s.nodes == nil {
		return "worker-0"
	}
	return s.nodes.pick()
}

// hitRate 累计前缀命中率(P6.5 扩缩决策输入;P7 换滑动窗口)。
func (s *Server) hitRate() float64 {
	total := s.totalBlocks.Load()
	if total == 0 {
		return 0
	}
	return float64(s.hitBlocks.Load()) / float64(total)
}

// lastReadyLatency 最近扩容决策→Ready 时延(原型:JoinShardNode 完成即 Ready)。
func (s *Server) LastReadyLatency() time.Duration {
	s.lastReadyLatencyMu.Lock()
	defer s.lastReadyLatencyMu.Unlock()
	return s.lastReadyLatency
}

// P6.4 handler 错误分型:保留 P3 的 HTTP 映射语义(不泄后端细节)。
type dispatchCallError struct{ err error }

func (e *dispatchCallError) Error() string { return e.err.Error() }

var errDispatchRejected = errors.New("dispatch rejected")

type generateCallError struct{ err error }

func (e *generateCallError) Error() string { return e.err.Error() }

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
