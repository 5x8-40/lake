package router

// P7.6(B3):本地命中率 workload harness——闭环度量「本地命中 = 执行节点 HBM
// 命中前缀 / 可复用前缀」(docs/features/slo.md「本地命中率 >40%(重复请求场景)」)。
//
// 拓扑:S 个会话 × T 轮驱动真实 handleChatCompletions;会话 = 共享系统前缀 +
// 逐轮累加历史;参数化节点数 / 会话漂移率 / 放置开关。worker client 是单例
// (多 worker 不在原型基建内)——度量在「Router + 镜像 + nodeRegistry N 逻辑
// 节点」层面成立:本地命中率是元数据/拓扑性质,由 Router 侧镜像记账得出
// (pickNode 决策 + hint.LocalHitBlocks),generate 路径保持 fake,这是忠实的
// (mock 字节不影响元数据真实性,仅放置滞后维度为上界)。
//
// 闭环:fake worker 执行后把 prompt 块注册进 p76PoolCP(L0=执行节点+L2)→
// Router 命中经 flushHotHits 按服务节点上报 ReportHits → p76PoolCP 的 B2
// 镜像简化版放置 → SubscribeView 广播 MOVED → Router 镜像看到新 L0 →
// 后续同前缀请求 LocalHitBlocks 增加。
//
// 对照组:policy 维度经 Server.pickFn 钩子切换——"rr" = RR 基线(替换钩子),
// "affinity" = 生产默认(nil 钩子 → pickNodeForRequest 亲和两段式,B1)。
// 断言:计数不变量全配置守;亲和各配置额外钉 slo.md「本地命中率 >40%」SLO 线
// (X5 收口,见 TestBenchP76LocalHitRate);RR 是陪跑对照不断言;绝对时延非门禁。

import (
	"context"
	"fmt"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	lakepb "github.com/chengda-wu/lake/go/pb"
	"google.golang.org/grpc"
)

// p76FollowThreshold 跟随流量触发阈值——与 Rust 权威实现
// rust/controlplane/src/placement.rs::FOLLOW_HIT_THRESHOLD 对齐(默认 2)。
const p76FollowThreshold = 2

// p76BlockState fake CP 内存权威视图中的块状态。
type p76BlockState struct {
	model string
	hash  []byte
	chain [][]byte // 注册时的前缀链(祖先共置用,对齐 Rust Entry.prefix_chain)
	l0    map[string]bool
	l2    string
}

// p76PoolCP:B2 放置逻辑的**镜像简化版**(Go 测试内闭环度量用;权威实现 =
// rust/controlplane/src/placement.rs,此处只保留度量所需语义):
//   - per-(block,node) 命中计数,过阈值(2)且块不在该节点 L0 → 对该节点放置
//     (滞回不重复;前缀祖先共置,根→叶)。
//   - HRW 预放置(B2-a)不在镜像内:稳态下生产节点即家节点,零动作;跨节点
//     偏差由跟随流量收敛,harness 度量不受影响。
//   - 计数节流(单次评估 top 16)不镜像:harness 每轮一次 flush,上报量远低。
type p76PoolCP struct {
	lakepb.UnimplementedControlPlaneServiceServer

	mu          sync.Mutex
	seq         uint64
	blocks      map[string]*p76BlockState // key: model\x00hash
	hits        map[string]map[string]uint32
	marked      map[string]map[string]bool
	placementOn bool

	subs   map[int]chan *lakepb.ViewUpdate
	nextID int
}

func newP76PoolCP(placementOn bool) *p76PoolCP {
	return &p76PoolCP{
		blocks:      make(map[string]*p76BlockState),
		hits:        make(map[string]map[string]uint32),
		marked:      make(map[string]map[string]bool),
		placementOn: placementOn,
		subs:        make(map[int]chan *lakepb.ViewUpdate),
	}
}

func p76Key(model string, hash []byte) string { return model + "\x00" + string(hash) }

func (c *p76PoolCP) currentSeq() uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.seq
}

// locationsOf 全量位置(L0 节点有序 + L2),MOVED/REGISTERED 事件携带。
func locationsOf(st *p76BlockState) []*lakepb.Location {
	l0 := make([]string, 0, len(st.l0))
	for n := range st.l0 {
		l0 = append(l0, n)
	}
	sort.Strings(l0)
	locs := make([]*lakepb.Location, 0, len(l0)+1)
	for _, n := range l0 {
		locs = append(locs, &lakepb.Location{Tier: lakepb.Tier_L0, NodeId: n, SegmentId: 1})
	}
	locs = append(locs, &lakepb.Location{Tier: lakepb.Tier_L2, NodeId: st.l2, SegmentId: 1})
	return locs
}

// broadcast 提交一批事件为一个带序号 ViewUpdate 并推给全部订阅者(持锁调用)。
func (c *p76PoolCP) broadcast(events ...*lakepb.ViewEvent) {
	if len(events) == 0 {
		return
	}
	c.seq++
	u := &lakepb.ViewUpdate{Seq: c.seq, Events: events}
	for _, ch := range c.subs {
		select {
		case ch <- u:
		default: // 订阅者积压:丢更新 → harness 的镜像收敛 waitFor 会超时暴露
		}
	}
}

// registerBlocks fake worker 执行后注册 prompt 块(测试内直调,与 proto
// RegisterBlocks 路径等价):新块落 L0=执行节点 + L2,已存在块不动
// (重复执行不产新 KV;跨节点偏差由跟随流量放置收敛)。
func (c *p76PoolCP) registerBlocks(model string, hashes [][]byte, execNode string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	var events []*lakepb.ViewEvent
	for i, h := range hashes {
		k := p76Key(model, h)
		if _, ok := c.blocks[k]; ok {
			continue
		}
		st := &p76BlockState{
			model: model,
			hash:  append([]byte(nil), h...),
			chain: append([][]byte(nil), hashes[:i+1]...),
			l0:    map[string]bool{execNode: true},
			l2:    "nvme-0",
		}
		c.blocks[k] = st
		events = append(events, &lakepb.ViewEvent{
			Kind:      lakepb.ViewEvent_REGISTERED,
			Id:        &lakepb.KVBlockID{ModelId: model, PoolKind: lakepb.PoolKind_TARGET, BlockHash: st.hash},
			Locations: locationsOf(st),
			L3Present: false,
		})
	}
	c.broadcast(events...)
}

// LookupPrefix 权威回退档(镜像 total-miss 时 Router 回查):沿链走,LocalHit =
// 块在 RequesterNodeId 的 L0。
func (c *p76PoolCP) LookupPrefix(_ context.Context, req *lakepb.LookupPrefixRequest) (*lakepb.LookupPrefixResponse, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	resp := &lakepb.LookupPrefixResponse{}
	allLocal := true
	for _, h := range req.GetPrefixHashes() {
		st, ok := c.blocks[p76Key(req.GetModelId(), h)]
		if !ok {
			allLocal = false
			break
		}
		local := st.l0[req.GetRequesterNodeId()]
		resp.Blocks = append(resp.Blocks, &lakepb.ReusableBlock{
			Id:       &lakepb.KVBlockID{ModelId: req.GetModelId(), PoolKind: lakepb.PoolKind_TARGET, BlockHash: st.hash},
			LocalHit: local,
		})
		resp.HitLength++
		allLocal = allLocal && local
	}
	resp.AllLocalHit = resp.HitLength > 0 && allLocal
	return resp, nil
}

// ReportHits:B2(b) 跟随流量镜像简化版——per-(block,node) 计数,过阈值且块
// 不在该节点 L0 → 放置该块及其未放置祖先链(滞回不重复),MOVED 广播。
func (c *p76PoolCP) ReportHits(_ context.Context, req *lakepb.ReportHitsRequest) (*lakepb.Ack, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	node := req.GetNodeId()
	var events []*lakepb.ViewEvent
	placed := make(map[string]bool) // 本次去重(跨链共享祖先)
	for _, id := range req.GetIds() {
		k := p76Key(id.GetModelId(), id.GetBlockHash())
		st, ok := c.blocks[k]
		if !ok {
			continue // 未注册块跳过(best-effort,对齐 Rust)
		}
		if c.hits[k] == nil {
			c.hits[k] = make(map[string]uint32)
		}
		c.hits[k][node]++
		if !c.placementOn || node == "" || c.hits[k][node] < p76FollowThreshold {
			continue
		}
		if st.l0[node] || c.marked[k][node] {
			continue
		}
		// 前缀祖先共置:根→叶补放未在该节点 L0 的祖先(D-direct 需要整链)。
		for _, anc := range st.chain {
			ak := p76Key(id.GetModelId(), anc)
			ast, ok := c.blocks[ak]
			if !ok || ast.l0[node] || placed[ak] {
				continue
			}
			placed[ak] = true
			ast.l0[node] = true
			if c.marked[ak] == nil {
				c.marked[ak] = make(map[string]bool)
			}
			c.marked[ak][node] = true
			events = append(events, &lakepb.ViewEvent{
				Kind:      lakepb.ViewEvent_MOVED,
				Id:        &lakepb.KVBlockID{ModelId: id.GetModelId(), PoolKind: lakepb.PoolKind_TARGET, BlockHash: ast.hash},
				Locations: locationsOf(ast),
			})
		}
	}
	c.broadcast(events...)
	return &lakepb.Ack{Ok: true}, nil
}

// SubscribeView 快照 + 直播(对齐 Rust CP P6.2 语义):快照(Seq=0,全量
// REGISTERED)+ 锚点(当前 seq),随后按提交序直播增量。resume 也直接给
// 全量快照——镜像 Apply(Seq=0) 重置重建,始终正确。
func (c *p76PoolCP) SubscribeView(
	_ *lakepb.SubscribeRequest,
	stream grpc.ServerStreamingServer[lakepb.ViewUpdate],
) error {
	c.mu.Lock()
	var snapshot []*lakepb.ViewEvent
	keys := make([]string, 0, len(c.blocks))
	for k := range c.blocks {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		st := c.blocks[k]
		snapshot = append(snapshot, &lakepb.ViewEvent{
			Kind:      lakepb.ViewEvent_REGISTERED,
			Id:        &lakepb.KVBlockID{ModelId: st.model, PoolKind: lakepb.PoolKind_TARGET, BlockHash: st.hash},
			Locations: locationsOf(st),
		})
	}
	id := c.nextID
	c.nextID++
	ch := make(chan *lakepb.ViewUpdate, 65536)
	c.subs[id] = ch
	anchor := c.seq
	c.mu.Unlock()
	defer func() {
		c.mu.Lock()
		delete(c.subs, id)
		c.mu.Unlock()
	}()

	if err := stream.Send(&lakepb.ViewUpdate{Seq: 0, Events: snapshot}); err != nil {
		return err
	}
	if err := stream.Send(&lakepb.ViewUpdate{Seq: anchor}); err != nil {
		return err
	}
	for {
		select {
		case u := <-ch:
			if err := stream.Send(u); err != nil {
				return err
			}
		case <-stream.Context().Done():
			return nil
		}
	}
}

// p76Harness 一次矩阵运行的产物:本地命中率与计数。
type p76Harness struct {
	srv *Server
	cp  *p76PoolCP
}

// newP76Harness 起 N 逻辑节点 + fake worker/agent + p76PoolCP 的完整闭环。
// worker client 单例:多节点由 nodeRegistry + RequesterNodeId 表达(见文件头注释)。
func newP76Harness(t *testing.T, nNodes int, placementOn bool) *p76Harness {
	t.Helper()
	cp := newP76PoolCP(placementOn)
	srv := dialFakeCP(t, cp)
	srv.cfg = Config{NodeRole: string(RoleHybrid), MaxInFlight: 256}
	srv.sched = NewPQScheduler(256, 30*time.Second)
	schedCtx, schedCancel := context.WithCancel(context.Background())
	t.Cleanup(schedCancel)
	go srv.sched.Run(schedCtx)
	ids := make([]string, nNodes)
	for i := range ids {
		ids[i] = fmt.Sprintf("worker-%d", i)
	}
	srv.nodes = newNodeRegistry(ids...)
	srv.hot = newHotSet(65536)
	srv.agent = fakeAgent{dispatch: func(context.Context, *lakepb.DispatchRequest) (*lakepb.Ack, error) {
		return &lakepb.Ack{Ok: true}, nil
	}}
	srv.worker = fakeWorker{generate: func(_ context.Context, req *lakepb.GenerateRequest) (*lakepb.GenerateResponse, error) {
		// fake 执行:产出 KV 注册进池(L0=执行节点 + L2 durable);
		// reused 回显 Router 下发的 hint(镜像记账即本 harness 的"真实"命中)。
		hashes := ChainBlockHashes(req.GetPromptTokens(), BlockSize)
		cp.registerBlocks(req.GetModelId(), hashes, req.GetRequesterNodeId())
		return &lakepb.GenerateResponse{
			RequestId:     req.GetRequestId(),
			Mode:          req.GetExecMode(),
			ReusedBlocks:  req.GetReusedBlocks(),
			PrefillBlocks: uint32(len(hashes)) - req.GetReusedBlocks(),
		}, nil
	}}
	return &p76Harness{srv: srv, cp: cp}
}

const p76SystemPrefix = "lake-p76-shared-system-prefix:你是 lake 测试助手,请简洁作答。"

// p76SessionPrompt 会话第 r 轮 prompt:共享系统前缀 + 逐轮累加历史(每轮一句)。
func p76SessionPrompt(session, round int) string {
	var b strings.Builder
	b.WriteString(p76SystemPrefix)
	for i := 0; i <= round; i++ {
		fmt.Fprintf(&b, "\nsession-%d-turn-%d:请介绍一下第 %d 个话题的更多细节。", session, i, i)
	}
	return b.String()
}

func postChatP76(t *testing.T, srv *Server, body, sessionKey string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
	if sessionKey != "" {
		req.Header.Set("X-Lake-Session-Id", sessionKey)
	}
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	return rec
}

// runP76Workload 跑一组配置,返回本地命中率与计数。逐轮同步驱动(确定性):
// 每轮结束 flushHotHits(触发 fake CP 放置)并等镜像收敛,再进下一轮。
// policy = "rr" 走 RR 基线(pickFn 钩子替换);其它值 = 生产默认亲和两段式。
func runP76Workload(
	t *testing.T,
	policy string,
	nNodes int,
	drift float64,
	placementOn bool,
	sessions, rounds int,
	seed int64,
) (rate float64, reusable, localHit uint64) {
	t.Helper()
	h := newP76Harness(t, nNodes, placementOn)
	if policy == "rr" {
		h.srv.pickFn = func(string, [][]byte, []byte) string { return h.srv.pickNode() }
	}
	rnd := rand.New(rand.NewSource(seed))

	for r := 0; r < rounds; r++ {
		for si := 0; si < sessions; si++ {
			// 漂移:以概率 drift 换新会话键(模拟客户端漂移/重连换键)——
			// 亲和策略的 HRW 路由键随之改变,会话亲和被打断;RR 不读该头。
			key := fmt.Sprintf("session-%d", si)
			if rnd.Float64() < drift {
				key = fmt.Sprintf("drift-%d-%d", si, r)
			}
			body := fmt.Sprintf(
				`{"model":"m","messages":[{"role":"user","content":%q}],"max_tokens":4}`,
				p76SessionPrompt(si, r),
			)
			rec := postChatP76(t, h.srv, body, key)
			if rec.Code != http.StatusOK {
				t.Fatalf("session %d round %d: status=%d body=%q", si, r, rec.Code, rec.Body.String())
			}
		}
		// 轮屏障:命中上报 → fake CP 放置 → 等镜像收敛再进下一轮。
		h.srv.flushHotHits(context.Background())
		want := h.cp.currentSeq()
		waitFor(t, 5*time.Second, fmt.Sprintf("round %d 镜像收敛到 seq %d", r, want), func() bool {
			return h.srv.Mirror().LastSeq() >= want
		})
	}
	return h.srv.LocalHitRate(), h.srv.ReusableBlockCount(), h.srv.LocalHitBlockCount()
}

// TestBenchP76LocalHitRate 敏感性表:策略(RR/亲和)× 节点数 × 漂移率 × 放置开关。
// 门禁口径:全配置守计数不变量;亲和配置额外断言 rate ≥ 0.4(slo.md SLO 声称,
// X5 收口为回归门禁);RR 基线是陪跑对照,不断言比率。
func TestBenchP76LocalHitRate(t *testing.T) {
	const (
		// localHitSLOGate 与 docs/features/slo.md「本地命中率 >40%」耦合,
		// SLO 收紧时须手动同步(门禁不会自动跟随)。
		localHitSLOGate = 0.4
		// 会话数取质数(7):避免 S%N==0 时 RR 把会话钉死在同一节点的混叠
		// 伪亲和(那会把 RR 基线测成隐式亲和);质数会话数让 RR 真正轮转。
		sessions = 7
		rounds   = 6
		seed     = 76
	)
	t.Log("P7.6 本地命中率(原型/mock,元数据口径):")
	t.Log("| policy | nodes | drift | placement | reusable | local_hit | rate |")
	t.Log("|--------|-------|-------|-----------|----------|-----------|------|")
	type row struct {
		policy  string
		nodes   int
		drift   float64
		placeOn bool
	}
	for _, cfg := range []row{
		{"rr", 1, 0.0, false}, {"rr", 1, 0.0, true},
		{"rr", 2, 0.0, false}, {"rr", 2, 0.0, true},
		{"rr", 4, 0.0, false}, {"rr", 4, 0.0, true},
		{"rr", 2, 0.3, false}, {"rr", 2, 0.3, true},
		{"rr", 4, 0.3, false}, {"rr", 4, 0.3, true},
		{"affinity", 1, 0.0, false}, {"affinity", 1, 0.0, true},
		{"affinity", 2, 0.0, false}, {"affinity", 2, 0.0, true},
		{"affinity", 4, 0.0, false}, {"affinity", 4, 0.0, true},
		{"affinity", 2, 0.3, false}, {"affinity", 2, 0.3, true},
		{"affinity", 4, 0.3, false}, {"affinity", 4, 0.3, true},
	} {
		rate, reusable, localHit := runP76Workload(t, cfg.policy, cfg.nodes, cfg.drift, cfg.placeOn, sessions, rounds, seed)
		t.Logf("| %s | %d | %.1f | %v | %d | %d | %.3f |",
			cfg.policy, cfg.nodes, cfg.drift, cfg.placeOn, reusable, localHit, rate)
		// 不变量(宽断言):计数一致 + 率有界。
		if localHit > reusable {
			t.Fatalf("localHit %d > reusable %d(计数不一致)", localHit, reusable)
		}
		if rate < 0 || rate > 1 {
			t.Fatalf("rate = %.3f 越界", rate)
		}
		if reusable == 0 {
			t.Fatal("reusable = 0(共享前缀/历史应产生复用)")
		}
		if cfg.nodes == 1 && rate < 0.99 {
			t.Fatalf("单节点本地命中率 = %.3f,应 ≈1(全部块都在唯一节点 L0)", rate)
		}
		// X5 收口:>40% 是 docs/features/slo.md 的 SLO 声称(亲和选路,重复请求
		// 场景),此断言将其从 t.Log 测量值升级为 CI 回归门禁。实测亲和全配置
		// 0.905–1.000、RR 基线 ≤0.471,0.4 线区分度干净且余量充足(>2x)。
		// RR 是陪跑对照,不断言;mock 绝对时延/吞吐仍非门禁。
		// 耦合注意:门禁值与 slo.md「本地命中率 >40%」手写同步——SLO 收紧时
		// 本常量不会自动跟随,改 SLO 须同步改这里。
		if cfg.policy == "affinity" && rate < localHitSLOGate {
			t.Fatalf("亲和本地命中率 = %.3f < %.1f(slo.md SLO 回归门禁,nodes=%d drift=%.1f placement=%v)",
				rate, localHitSLOGate, cfg.nodes, cfg.drift, cfg.placeOn)
		}
	}
}
