package router

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	lakepb "github.com/chengda-wu/lake/go/pb"
)

// P6.5:基于指标的弹性扩缩容(issue #52)——Router 按队列深度/命中率等指标做
// worker 扩缩决策;Drain 复用 P4.9 shard 逻辑(DrainShardNode 推 L2 计划),
// 扩容 JoinShardNode + 一致性哈希最小迁移(环最小迁移 Rust 侧
// shard.rs::expand_moves_only_new_interval 已验证)。
//
// 边界:
//   - 决策只发 CP RPC + 维护路由注册表;真实 provision(起 worker 进程/镜像)
//     归外部编排(K8s),扩容决策→Ready <10s 目标待 P7 校准——原型以
//     JoinShardNode 完成记 Ready(注册表可路由)。
//   - 缩容不丢请求:draining 节点立即摘出路由表(新请求不再进入),
//     在途请求跑完;KV 由 Drain 推 L2 计划兜底(池权威,不丢)。
//   - 默认关闭(LAKE_AUTOSCALE=1 开启)——单进程原型 deploy 不起真实 worker,
//     决策层 + CP 接线由单测闭环(issue defer:真跨机联调归 P5)。

// MetricsSnapshot 扩缩决策输入(Router 自观测;TTFT/ITL 原型预留,P7 接入)。
type MetricsSnapshot struct {
	QueueLen     int     // PQScheduler 排队长度
	InFlight     int     // 在途执行数
	RemainingCap int     // 剩余执行容量
	HitRate      float64 // 累计前缀命中率(reused_blocks/total_blocks),[0,1]
}

// ScaleDecision 扩缩决策。
type ScaleDecision int

const (
	DecideNone ScaleDecision = iota
	DecideScaleOut
	DecideScaleIn
)

func (d ScaleDecision) String() string {
	switch d {
	case DecideScaleOut:
		return "scale_out"
	case DecideScaleIn:
		return "scale_in"
	default:
		return "none"
	}
}

// AutoscaleConfig 阈值与防抖参数。
type AutoscaleConfig struct {
	MinNodes         int           // 下限(默认 1:worker-0 永不被缩掉)
	MaxNodes         int           // 上限(默认 8)
	ScaleOutQueueLen int           // 队列深度 ≥ 此值持续 SustainPeriods → 扩
	ScaleInQueueLen  int           // 队列 ≤ 此值且 InFlight ≤ 1 持续 SustainPeriods → 缩
	SustainPeriods   int           // 需持续的评估周期数(防抖)
	Cooldown         time.Duration // 两次动作最小间隔
}

func (c *AutoscaleConfig) withDefaults() AutoscaleConfig {
	out := *c
	if out.MinNodes <= 0 {
		out.MinNodes = 1
	}
	if out.MaxNodes <= 0 {
		out.MaxNodes = 8
	}
	if out.ScaleOutQueueLen <= 0 {
		out.ScaleOutQueueLen = 4
	}
	if out.SustainPeriods <= 0 {
		out.SustainPeriods = 3
	}
	if out.Cooldown <= 0 {
		out.Cooldown = 10 * time.Second
	}
	return out
}

// Autoscaler 纯决策器(状态 = 连续超阈值计数 + 上次动作时刻)。
type Autoscaler struct {
	cfg         AutoscaleConfig
	overStreak  int
	underStreak int
	lastAction  time.Time
}

func NewAutoscaler(cfg AutoscaleConfig) *Autoscaler {
	return &Autoscaler{cfg: cfg.withDefaults()}
}

// Evaluate 决策:持续超阈值 SustainPeriods 个周期且过 cooldown 才动作;
// 中间值清零计数(防抖)。nodeCount 为当前 ready 节点数。
func (a *Autoscaler) Evaluate(now time.Time, m MetricsSnapshot, nodeCount int) ScaleDecision {
	over := m.QueueLen >= a.cfg.ScaleOutQueueLen
	under := m.QueueLen <= a.cfg.ScaleInQueueLen && m.InFlight <= 1
	switch {
	case over:
		a.overStreak++
		a.underStreak = 0
	case under:
		a.underStreak++
		a.overStreak = 0
	default:
		a.overStreak = 0
		a.underStreak = 0
	}
	if now.Sub(a.lastAction) < a.cfg.Cooldown {
		return DecideNone
	}
	if a.overStreak >= a.cfg.SustainPeriods && nodeCount < a.cfg.MaxNodes {
		a.overStreak = 0
		a.lastAction = now
		return DecideScaleOut
	}
	if a.underStreak >= a.cfg.SustainPeriods && nodeCount > a.cfg.MinNodes {
		a.underStreak = 0
		a.lastAction = now
		return DecideScaleIn
	}
	return DecideNone
}

// nodeRegistry Router 侧路由节点表(ready 有序;draining 摘出路由等摘除)。
type nodeRegistry struct {
	mu       sync.RWMutex
	ready    []string
	draining map[string]bool
	seq      int // 已分配的最大节点序号(worker-N)
	rr       int // pick 轮询游标
	// P7.6(B1):per-node 在途请求数(atomic,免锁读)——亲和护栏与加权 HRW
	// 的负载信号。注意区别于 loadsync.go 的出站上报:那是 Router→agent 的
	// 集群级遥测,这里的计数是选路热路径的即时本地信号。
	inflight map[string]*atomic.Int64
}

func newNodeRegistry(initial ...string) *nodeRegistry {
	r := &nodeRegistry{draining: make(map[string]bool), inflight: make(map[string]*atomic.Int64)}
	for _, id := range initial {
		r.ready = append(r.ready, id)
		r.inflight[id] = &atomic.Int64{}
		var n int
		if _, err := fmt.Sscanf(id, "worker-%d", &n); err == nil && n > r.seq {
			r.seq = n
		}
	}
	return r
}

// readyIDs ready 节点有序快照(排序保证亲和扫描/HRW tie-break 与池侧
// rust/controlplane ready_nodes 同序,跨语言确定性一致)。
func (r *nodeRegistry) readyIDs() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := append([]string(nil), r.ready...)
	sort.Strings(out)
	return out
}

// incInFlight/decInFlight 在途记账:选路即占 +1、请求完成 -1(含排队与
// 重试在途;server.go handleChatCompletions,非 dispatch 边界)
// (调用方 defer);draining 节点在途照计(跑完才摘除)。
func (r *nodeRegistry) incInFlight(id string) { r.slot(id).Add(1) }
func (r *nodeRegistry) decInFlight(id string) { r.slot(id).Add(-1) }

func (r *nodeRegistry) inFlight(id string) int64 { return r.slot(id).Load() }

func (r *nodeRegistry) slot(id string) *atomic.Int64 {
	r.mu.RLock()
	s, ok := r.inflight[id]
	r.mu.RUnlock()
	if ok {
		return s
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if s, ok = r.inflight[id]; !ok {
		s = &atomic.Int64{}
		r.inflight[id] = s
	}
	return s
}

// pick 轮询取 ready 节点(空表兜底 worker-0)——P6.5 review:扩缩后请求
// 应在节点间分流(单节点时退化为恒等,单进程 deploy 行为不变)。
func (r *nodeRegistry) pick() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.ready) == 0 {
		return "worker-0"
	}
	id := r.ready[r.rr%len(r.ready)]
	r.rr++
	return id
}

func (r *nodeRegistry) count() int {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.ready)
}

func (r *nodeRegistry) nextID() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.seq++
	return fmt.Sprintf("worker-%d", r.seq)
}

func (r *nodeRegistry) add(id string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.draining, id)
	r.ready = append(r.ready, id)
}

// markDraining 摘出路由表(新请求不再进入;在途跑完)。返回 false = 不在 ready。
func (r *nodeRegistry) markDraining(id string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	for i, n := range r.ready {
		if n == id {
			r.ready = append(r.ready[:i], r.ready[i+1:]...)
			r.draining[id] = true
			return true
		}
	}
	return false
}

// remove 摘除(placement 已清,CP RemoveShardNode 成功后)。
func (r *nodeRegistry) remove(id string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.draining, id)
}

// unmarkDraining Drain 失败回滚(review #57):victim 回 ready 末位
// (LIFO 语义不变)——Drain RPC 未生效时容量不得永久丢一档。
func (r *nodeRegistry) unmarkDraining(id string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.draining[id] {
		delete(r.draining, id)
		r.ready = append(r.ready, id)
	}
}

// lastReady 最后加入的 ready 节点(LIFO 缩容 victim——缩最近扩出来的)。
func (r *nodeRegistry) lastReady() (string, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	if len(r.ready) == 0 {
		return "", false
	}
	return r.ready[len(r.ready)-1], true
}

// hotSet 命中观测窗(按服务节点分桶,有界 FIFO,窗内去重)——P7 收口(方案 Z):
// Router 只作热度传感器,命中批量上报 CP(ReportHits → radix hit_count);
// 扩容 warmup / 预放置的选块与发起归池侧,Router 不指挥放置。
// P7.6(B2):命中按服务节点分桶——ReportHits.node_id = 命中流量的服务节点
// (跟随流量放置的目标节点),不再笼统上报 "router"。
type hotSet struct {
	mu      sync.Mutex
	cap     int
	seen    map[string]struct{} // node\x00model\x00hash,窗内去重
	pending []hotHit            // 自上次 drain 以来的新命中
}

// hotHit 一条待上报命中:服务节点 + 命中块。
type hotHit struct {
	node string
	id   *lakepb.KVBlockID
}

func newHotSet(cap int) *hotSet {
	if cap <= 0 {
		cap = 256
	}
	return &hotSet{cap: cap, seen: make(map[string]struct{})}
}

func hotKey(node string, id *lakepb.KVBlockID) string {
	return node + "\x00" + id.GetModelId() + "\x00" + string(id.GetBlockHash())
}

func (h *hotSet) add(node string, ids ...*lakepb.KVBlockID) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, id := range ids {
		k := hotKey(node, id)
		if _, ok := h.seen[k]; ok {
			continue
		}
		h.seen[k] = struct{}{}
		h.pending = append(h.pending, hotHit{node: node, id: id})
	}
	// 窗有界:超 cap 摘最旧(未上报也丢——best-effort,CP 计数偏弱可容忍)。
	for len(h.pending) > h.cap {
		delete(h.seen, hotKey(h.pending[0].node, h.pending[0].id))
		h.pending = h.pending[1:]
	}
}

// drain 取出本窗命中并按服务节点分桶,开窗新窗。上报丢失不重发(best-effort)。
func (h *hotSet) drain() map[string][]*lakepb.KVBlockID {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make(map[string][]*lakepb.KVBlockID)
	for _, hit := range h.pending {
		out[hit.node] = append(out[hit.node], hit.id)
	}
	h.pending = nil
	h.seen = make(map[string]struct{})
	return out
}

func (r *nodeRegistry) drainingList() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]string, 0, len(r.draining))
	for id := range r.draining {
		out = append(out, id)
	}
	return out
}

// applyScale 执行决策:扩容 JoinShardNode(一致性哈希最小迁移,CP 返回迁移清单)
// → 节点入路由表(Ready);缩容 LIFO 选 victim → DrainShardNode(推 L2 计划)
// → 摘出路由 → RemoveShardNode(CP 在 placement 未清时拒绝,留 draining 下周期重试)。
func (s *Server) applyScale(ctx context.Context, d ScaleDecision) error {
	switch d {
	case DecideScaleOut:
		id := s.nodes.nextID()
		start := time.Now()
		resp, err := s.cp.JoinShardNode(ctx, &lakepb.JoinShardNodeRequest{NodeId: id})
		if err != nil {
			return fmt.Errorf("JoinShardNode: %w", err)
		}
		if !resp.GetOk() {
			return fmt.Errorf("JoinShardNode rejected: %s", resp.GetErr())
		}
		s.nodes.add(id)
		s.syncCapacity() // 并发随 ready 节点数升(review #57)
		// 原型:join 完成即 Ready(真实 provision 时延 <10s 目标待 P7 校准)
		s.lastReadyLatencyMu.Lock()
		s.lastReadyLatency = time.Since(start)
		s.lastReadyLatencyMu.Unlock()
		slog.Info("scale-out: node ready",
			"node", id, "migrations", resp.GetMigrationCount(), "ring_gen", resp.GetMap().GetGeneration())
		// P7 收口(方案 Z):新节点 warmup 由池侧自主决策发起——CP 在
		// JoinShardNode 后按 hit_count(ReportHits 喂入)选热块并下发
		// PlaceBlocks;Router 不指挥放置,只持续上报命中(autoscaleTick 尾部)。
		// P7.4 注:原 Router 侧 prefetch 时延探针随归属迁移移除——warmup
		// 时延观测归池侧(CP warmup 计划 + P5 真字节路径),冷启动瀑布的
		// KV 段改由池侧埋点(见 bench/coldstart_waterfall.py 口径注记)。
	case DecideScaleIn:
		victim, ok := s.nodes.lastReady()
		if !ok {
			return nil
		}
		if !s.nodes.markDraining(victim) {
			return nil
		}
		resp, err := s.cp.DrainShardNode(ctx, &lakepb.DrainShardNodeRequest{NodeId: victim})
		if err != nil {
			s.nodes.unmarkDraining(victim) // Drain 未执行:回滚,容量不丢
			s.syncCapacity()
			return fmt.Errorf("DrainShardNode: %w", err)
		}
		if !resp.GetOk() {
			s.nodes.unmarkDraining(victim)
			s.syncCapacity()
			return fmt.Errorf("DrainShardNode rejected: %s", resp.GetErr())
		}
		s.syncCapacity() // victim 摘出路由 → 并发随节点数降
		slog.Info("scale-in: drain node",
			"node", victim, "migrations", resp.GetMigrationCount(), "push_l2", len(resp.GetPushL2()))
		s.reapDraining(ctx)
	}
	return nil
}

// syncCapacity 并发随 ready 节点数伸缩(review #57):总并发 = 单节点上限 ×
// ready 数——扩容才真加执行容量,否则只动 shard 环不分流。单节点时恒等。
func (s *Server) syncCapacity() {
	if s.sched == nil {
		return
	}
	base := s.cfg.MaxInFlight
	if base <= 0 {
		base = 1
	}
	s.sched.SetMaxInFlight(base * s.nodes.count())
}

// reapDraining 对 draining 节点重试 RemoveShardNode(CP 以 locations 清空为
// 完成门——对齐 Mooncake unmount 与副本生命周期;未清则留待下周期)。
func (s *Server) reapDraining(ctx context.Context) {
	for _, id := range s.nodes.drainingList() {
		ack, err := s.cp.RemoveShardNode(ctx, &lakepb.RemoveShardNodeRequest{NodeId: id})
		if err == nil && ack.GetOk() {
			s.nodes.remove(id)
			slog.Info("scale-in: node removed from shard ring", "node", id)
		}
	}
}

// autoscaleTick 一个评估周期:先收割 draining,再按指标决策执行。
func (s *Server) autoscaleTick(ctx context.Context) {
	s.reapDraining(ctx)
	// P7 收口(方案 Z):命中观测批量上报 CP(radix hit_count),供池侧
	// 扩容 warmup / 未来预放置选块;Router 只报告、不指挥放置。
	// 先于 applyScale 上报:同 tick 的命中计数能喂进本次 Join 的 warmup_plan。
	s.flushHotHits(ctx)
	if !s.cfg.Autoscale {
		return // 命中上报不随扩缩容开关;评估/执行才被开关门控
	}
	snap := s.sched.LoadSnapshot("router")
	m := MetricsSnapshot{
		QueueLen:     int(snap.GetQueueLen()),
		InFlight:     int(snap.GetInFlight()),
		RemainingCap: int(snap.GetRemainingCap()),
		HitRate:      s.hitRate(),
	}
	d := s.scaler.Evaluate(time.Now(), m, s.nodes.count())
	if d != DecideNone {
		slog.Info("autoscale decision", "metrics", m, "nodes", s.nodes.count(), "decision", d)
		if err := s.applyScale(ctx, d); err != nil {
			slog.Error("autoscale apply failed", "decision", d, "err", err)
		}
	}
}

// flushHotHits 本窗命中按服务节点分桶批量上报 CP(best-effort:失败不重发,
// CP 计数偏弱可容忍)。NodeId = 命中流量的服务节点(P7.6 B2 跟随流量放置目标)。
func (s *Server) flushHotHits(ctx context.Context) {
	if s.hot == nil {
		return
	}
	byNode := s.hot.drain()
	for node, ids := range byNode {
		if node == "" || len(ids) == 0 {
			continue
		}
		if _, err := s.cp.ReportHits(ctx, &lakepb.ReportHitsRequest{NodeId: node, Ids: ids}); err != nil {
			// best-effort(失败不重发)——可容忍失败降级 Warn,不打 Error。
			slog.Warn("report hits failed", "node", node, "ids", len(ids), "err", err)
		}
	}
}

// runAutoscale 后台周期循环(New 无条件启动):命中上报常做,扩缩容评估/执行由 Config.Autoscale 门控。
func (s *Server) runAutoscale(ctx context.Context, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.autoscaleTick(ctx)
		}
	}
}
