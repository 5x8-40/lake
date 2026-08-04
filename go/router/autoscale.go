package router

import (
	"context"
	"fmt"
	"log"
	"sync"
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
}

func newNodeRegistry(initial ...string) *nodeRegistry {
	r := &nodeRegistry{draining: make(map[string]bool)}
	for _, id := range initial {
		r.ready = append(r.ready, id)
		var n int
		if _, err := fmt.Sscanf(id, "worker-%d", &n); err == nil && n > r.seq {
			r.seq = n
		}
	}
	return r
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

// hotSet 近期前缀命中块(有界 FIFO 去重)——P6.6:扩容新节点时 prefetch 热 KV
// 到其 HBM(权重预加载/layer-async 在 Python coldstart;本结构管"预热什么")。
type hotSet struct {
	mu    sync.Mutex
	cap   int
	seen  map[string]struct{} // model\x00hex(hash)
	order []*lakepb.KVBlockID // FIFO,超 cap 摘最旧
}

func newHotSet(cap int) *hotSet {
	if cap <= 0 {
		cap = 256
	}
	return &hotSet{cap: cap, seen: make(map[string]struct{})}
}

func hotKey(id *lakepb.KVBlockID) string {
	return id.GetModelId() + "\x00" + string(id.GetBlockHash())
}

func (h *hotSet) add(ids ...*lakepb.KVBlockID) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, id := range ids {
		k := hotKey(id)
		if _, ok := h.seen[k]; ok {
			continue
		}
		h.seen[k] = struct{}{}
		h.order = append(h.order, id)
	}
	for len(h.order) > h.cap {
		delete(h.seen, hotKey(h.order[0]))
		h.order = h.order[1:]
	}
}

func (h *hotSet) snapshot() []*lakepb.KVBlockID {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]*lakepb.KVBlockID(nil), h.order...)
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
		log.Printf("scale-out: %s ready (migrations=%d, ring_gen=%d)",
			id, resp.GetMigrationCount(), resp.GetMap().GetGeneration())
		// P6.6:KV prefetch——热块异步铺到新节点 HBM(不阻塞 Ready;
		// 真实字节搬运归 P5,此处走 agent PlaceBlocks 控制信令)。
		if s.hot != nil {
			if ids := s.hot.snapshot(); len(ids) > 0 {
				go func() {
					pctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
					defer cancel()
					ack, err := s.agent.PlaceBlocks(pctx, &lakepb.PlaceBlocksRequest{
						Ids: ids, TargetNodeId: id,
					})
					if err != nil {
						log.Printf("scale-out prefetch %s: %v", id, err)
					} else if !ack.GetOk() {
						log.Printf("scale-out prefetch %s rejected: %s", id, ack.GetErr())
					} else {
						log.Printf("scale-out prefetch: %d hot blocks → %s", len(ids), id)
					}
				}()
			}
		}
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
		log.Printf("scale-in: drain %s (migrations=%d, push_l2=%d)",
			victim, resp.GetMigrationCount(), len(resp.GetPushL2()))
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
			log.Printf("scale-in: %s removed from shard ring", id)
		}
	}
}

// autoscaleTick 一个评估周期:先收割 draining,再按指标决策执行。
func (s *Server) autoscaleTick(ctx context.Context) {
	s.reapDraining(ctx)
	snap := s.sched.LoadSnapshot("router")
	m := MetricsSnapshot{
		QueueLen:     int(snap.GetQueueLen()),
		InFlight:     int(snap.GetInFlight()),
		RemainingCap: int(snap.GetRemainingCap()),
		HitRate:      s.hitRate(),
	}
	d := s.scaler.Evaluate(time.Now(), m, s.nodes.count())
	if d != DecideNone {
		log.Printf("autoscale: %+v nodes=%d → %s", m, s.nodes.count(), d)
		if err := s.applyScale(ctx, d); err != nil {
			log.Printf("autoscale apply %s: %v", d, err)
		}
	}
}

// runAutoscale 后台评估循环(LAKE_AUTOSCALE=1 时由 New 启动)。
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
