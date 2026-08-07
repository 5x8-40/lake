package router

// P7.6(B1)亲和选路单测:镜像 L0 得分偏好、护栏降级、HRW 确定性与最小迁移、
// in-flight 记账。harness 级 RR/亲和对照在 bench_p76_test.go。

import (
	"context"
	"fmt"
	"math/rand"
	"net/http"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	lakepb "github.com/chengda-wu/lake/go/pb"
)

// newAffinityServer 裸 Server(无 CP/worker 依赖)供纯选路测试。
func newAffinityServer(nodeIDs ...string) *Server {
	return &Server{
		cfg:    Config{NodeRole: string(RoleHybrid), MaxInFlight: 64, AffinityInFlightGuard: 4},
		mirror: NewViewMirror(),
		nodes:  newNodeRegistry(nodeIDs...),
	}
}

// regL0 往镜像注册一块(model "m",L0 落在 nodes)。
func regL0(m *ViewMirror, seq uint64, hash string, nodes ...string) {
	locs := make([]*lakepb.Location, 0, len(nodes))
	for _, n := range nodes {
		locs = append(locs, &lakepb.Location{Tier: lakepb.Tier_L0, NodeId: n, SegmentId: 1})
	}
	if err := m.Apply(&lakepb.ViewUpdate{Seq: seq, Events: []*lakepb.ViewEvent{{
		Kind:      lakepb.ViewEvent_REGISTERED,
		Id:        &lakepb.KVBlockID{ModelId: "m", PoolKind: lakepb.PoolKind_TARGET, BlockHash: []byte(hash)},
		Locations: locs,
	}}}); err != nil {
		panic(err)
	}
}

func TestAffinityPrefersL0Node(t *testing.T) {
	s := newAffinityServer("worker-0", "worker-1", "worker-2")
	hashes := [][]byte{[]byte("h0"), []byte("h1"), []byte("h2")}
	// 整链在 worker-1 的 L0;worker-2 只有首块(得分 1);worker-0 零命中。
	regL0(s.mirror, 1, "h0", "worker-1", "worker-2")
	regL0(s.mirror, 2, "h1", "worker-1")
	regL0(s.mirror, 3, "h2", "worker-1")

	if got := s.pickNodeForRequest("m", hashes, []byte("key-x")); got != "worker-1" {
		t.Fatalf("亲和得分最高应为 worker-1, got %s", got)
	}
}

func TestAffinityGuardDowngradesOverloaded(t *testing.T) {
	s := newAffinityServer("worker-0", "worker-1", "worker-2")
	hashes := [][]byte{[]byte("h0"), []byte("h1")}
	regL0(s.mirror, 1, "h0", "worker-1", "worker-2")
	regL0(s.mirror, 2, "h1", "worker-1") // worker-1 得分 2,worker-2 得分 1

	// 得分最高者 in-flight 打满护栏(4)→ 降级到次优 worker-2。
	for i := 0; i < 4; i++ {
		s.nodes.incInFlight("worker-1")
	}
	if got := s.pickNodeForRequest("m", hashes, []byte("key-x")); got != "worker-2" {
		t.Fatalf("护栏降级应选次优 worker-2, got %s", got)
	}

	// 全部命中节点过载 → 冷路径:护栏过滤后只剩 worker-0 eligible(在途 0)。
	for i := 0; i < 4; i++ {
		s.nodes.incInFlight("worker-2")
	}
	if got := s.pickNodeForRequest("m", hashes, []byte("key-x")); got != "worker-0" {
		t.Fatalf("全过载应落冷路径(worker-1/2 被护栏过滤,只剩 worker-0), got %s", got)
	}
}

func TestAffinityColdHRWDeterministic(t *testing.T) {
	s := newAffinityServer("worker-0", "worker-1", "worker-2")
	// 空镜像(无命中)→ 全部走冷路径;同键同节点(纯函数)。
	for i := 0; i < 8; i++ {
		key := []byte(fmt.Sprintf("key-%d", i))
		first := s.pickNodeForRequest("m", nil, key)
		for j := 0; j < 4; j++ {
			if got := s.pickNodeForRequest("m", nil, key); got != first {
				t.Fatalf("key %q 第 %d 次选路漂移: %s → %s", key, j, first, got)
			}
		}
	}
	// 键空间应散布到 ≥2 节点(HRW 非恒等)。
	seen := map[string]bool{}
	for i := 0; i < 64; i++ {
		seen[s.pickNodeForRequest("m", nil, []byte(fmt.Sprintf("spread-%d", i)))] = true
	}
	if len(seen) < 2 {
		t.Fatalf("64 键只落在 %d 个节点,HRW 散布异常", len(seen))
	}
}

func TestAffinityHRWMinimalMigration(t *testing.T) {
	s := newAffinityServer("worker-0", "worker-1", "worker-2")
	rnd := rand.New(rand.NewSource(7))
	const keys = 2000
	before := make(map[string]string, keys)
	for i := 0; i < keys; i++ {
		k := string([]byte{byte(i), byte(i >> 8), byte(rnd.Intn(256))})
		before[k] = s.pickNodeForRequest("m", nil, []byte(k))
	}
	s.nodes.add("worker-3")
	moved := 0
	for k, prev := range before {
		if got := s.pickNodeForRequest("m", nil, []byte(k)); got != prev {
			moved++
		}
	}
	// HRW 性质:迁移 ≈ 1/(N+1) = 25%(3→4);宽界 35% 防实现退化成取模。
	if moved > keys*35/100 {
		t.Fatalf("加节点迁移 %d/%d (%.1f%%),超 HRW 宽界 35%%(期望 ≈25%%)",
			moved, keys, float64(moved)/keys*100)
	}
	if moved == 0 {
		t.Fatal("加节点零迁移——新节点未进入 HRW 候选")
	}
}

// TestAffinityColdPathMatchesRustHRW 钉死 Go 冷路径与池侧
// placement.rs::hrw_score 的同构选点:纯整数 argmax + 节点 id 决胜,
// 负载未过护栏时必须逐点同家(review #69:float64 加权会在精度碰撞/
// 负载不等时分叉)。
func TestAffinityColdPathMatchesRustHRW(t *testing.T) {
	s := newAffinityServer("worker-0", "worker-1", "worker-2")
	ids := []string{"worker-0", "worker-1", "worker-2"}
	for i := 0; i < 64; i++ {
		key := []byte(fmt.Sprintf("conv-%d", i))
		want, best := "", uint64(0)
		for _, id := range ids {
			score := fnv1a64(key, id)
			if want == "" || score > best || (score == best && id < want) {
				want, best = id, score
			}
		}
		if got := s.pickNodeForRequest("m", nil, key); got != want {
			t.Fatalf("key %q: 冷路径=%s,纯 HRW argmax=%s(分叉)", key, got, want)
		}
	}
}

// TestAffinityColdPathGuardOverflow 冷路径护栏:家节点在途 ≥ 护栏时溢到
// 次高分节点(确定性);全部过载退无过滤纯 HRW(回到家节点)。
func TestAffinityColdPathGuardOverflow(t *testing.T) {
	s := newAffinityServer("worker-0", "worker-1", "worker-2") // 护栏=4
	key := []byte("overflow-key")
	home := s.pickNodeForRequest("m", nil, key)

	s.nodes.slot(home).Store(4)
	second := s.pickNodeForRequest("m", nil, key)
	if second == home {
		t.Fatalf("家节点 %s 在途达护栏仍未溢出", home)
	}
	if again := s.pickNodeForRequest("m", nil, key); again != second {
		t.Fatalf("溢出选择不确定: %s → %s", second, again)
	}

	for _, id := range []string{"worker-0", "worker-1", "worker-2"} {
		s.nodes.slot(id).Store(4)
	}
	if got := s.pickNodeForRequest("m", nil, key); got != home {
		t.Fatalf("全过载应退纯 HRW 回家节点 %s,实得 %s", home, got)
	}
}

// TestFnv1a64CrossLanguageVectors 钉死与 Rust 权威(placement.rs::hrw_score)
// 的字节布局一致性:原语层 fnv1a 经 std lib 核对("lake" 向量与 Rust
// fnv1a64_matches_go_reference_vectors 同值),组合层钉绝对值防回归。
func TestFnv1a64CrossLanguageVectors(t *testing.T) {
	if got := fnv1a64([]byte("anchor-0"), "n0"); got != 0xc8e4e7b063e4ef7f {
		t.Fatalf("fnv1a64(anchor-0,n0) = %#x, want 0xc8e4e7b063e4ef7f", got)
	}
	if got := fnv1a64([]byte("key!"), "worker-1"); got != 0xd485e7e9b3d80a5f {
		t.Fatalf("fnv1a64(key!,worker-1) = %#x, want 0xd485e7e9b3d80a5f", got)
	}
}

// TestInFlightAccounting 全链路记账:请求执行中在途=1,完成后归零。
func TestInFlightAccounting(t *testing.T) {
	h := newP76Harness(t, 2, false)
	started := make(chan string, 1)
	release := make(chan struct{})
	var once sync.Once
	h.srv.worker = fakeWorker{generate: func(_ context.Context, req *lakepb.GenerateRequest) (*lakepb.GenerateResponse, error) {
		once.Do(func() { started <- req.GetRequesterNodeId() })
		<-release
		hashes := ChainBlockHashes(req.GetPromptTokens(), BlockSize)
		h.cp.registerBlocks(req.GetModelId(), hashes, req.GetRequesterNodeId())
		return &lakepb.GenerateResponse{RequestId: req.GetRequestId(), Mode: req.GetExecMode()}, nil
	}}

	body := `{"model":"m","messages":[{"role":"user","content":"inflight 记账探针请求"}],"max_tokens":2}`
	done := make(chan int, 1)
	go func() {
		rec := postChatP76(t, h.srv, body, "")
		done <- rec.Code
	}()

	var node string
	select {
	case node = <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("worker 未收到请求")
	}
	waitFor(t, 2*time.Second, "在途计数升 1", func() bool {
		return h.srv.nodes.inFlight(node) == 1
	})
	close(release)
	if code := <-done; code != http.StatusOK {
		t.Fatalf("status = %d", code)
	}
	waitFor(t, 2*time.Second, "在途计数归零", func() bool {
		return h.srv.nodes.inFlight(node) == 0
	})
}

// TestInFlightVisibleWhileQueued 护栏看得见排队中的亲和负载(review #69
// High):1 槽调度器下 req1 执行中、req2 仍排队(未 dispatch)时,req2
// 选中节点的 in-flight 已计入——原实现 dispatch 前才 +1,排队期对护栏
// 不可见(并发 pick 同读低载 → 涌向同一热点,TOCTOU + queue-blind)。
func TestInFlightVisibleWhileQueued(t *testing.T) {
	h := newP76Harness(t, 2, false)
	// 换 1 槽调度器,req2 必排队(原 256 槽 goroutine 闲置,随 cleanup 取消)。
	schedCtx, schedCancel := context.WithCancel(context.Background())
	t.Cleanup(schedCancel)
	h.srv.sched = NewPQScheduler(1, 30*time.Second)
	go h.srv.sched.Run(schedCtx)

	release := make(chan struct{})
	var started atomic.Int32
	h.srv.worker = fakeWorker{generate: func(_ context.Context, req *lakepb.GenerateRequest) (*lakepb.GenerateResponse, error) {
		started.Add(1)
		<-release
		hashes := ChainBlockHashes(req.GetPromptTokens(), BlockSize)
		h.cp.registerBlocks(req.GetModelId(), hashes, req.GetRequesterNodeId())
		return &lakepb.GenerateResponse{RequestId: req.GetRequestId(), Mode: req.GetExecMode()}, nil
	}}

	body := `{"model":"m","messages":[{"role":"user","content":"队列可见性探针"}],"max_tokens":2}`
	done := make(chan int, 2)
	go func() { done <- postChatP76(t, h.srv, body, "").Code }()
	waitFor(t, 2*time.Second, "req1 开始执行并卡住", func() bool { return started.Load() == 1 })

	go func() { done <- postChatP76(t, h.srv, body, "").Code }()
	// req2 已 pick 并入队(未 dispatch):全节点 in-flight 总和应升为 2。
	waitFor(t, 2*time.Second, "排队中的请求已计入在途", func() bool {
		return h.srv.nodes.inFlight("worker-0")+h.srv.nodes.inFlight("worker-1") == 2
	})
	if got := started.Load(); got != 1 {
		t.Fatalf("req2 不应已 dispatch(started=%d)", got)
	}
	close(release)
	if code := <-done; code != http.StatusOK {
		t.Fatalf("req1 status = %d", code)
	}
	if code := <-done; code != http.StatusOK {
		t.Fatalf("req2 status = %d", code)
	}
}
