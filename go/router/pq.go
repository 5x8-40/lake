package router

import (
	"context"
	"errors"
	"sort"
	"sync"
	"time"

	lakepb "github.com/chengda-wu/lake/go/pb"
)

// P6.4:Router 侧优先级调度(issue #52)——priority queuing 决定**已准入**请求的
// 执行顺序与抢占;不丢请求、不做 shedding(入口准入/限并发/按优先级丢弃归 gateway,
// docs/features/slo.md「过载控制」;docs/architecture/scheduling.md §1/§3)。
//
// 语义:
//   - 队列无界:Submit 永不因负载拒绝(无 shedding 越界);maxInFlight 只限并发执行数。
//   - 排序:priority 大者先执行;同 priority 按提交序 FIFO。
//   - 抢占:在途已满且新请求 priority 严格高于在途最低者 → 以 errPreempted 取消
//     在途最低者并**重排回队列**(原 seq 不变,同优先级最前)。被抢占者 KV 在存储池
//     保留,重跑命中前缀,代价是一次重算——对齐 vLLM v1 抢占重算式
//     (scheduling.md「DP 间在途再均衡」),lake 在 Router 层做同构 cancel+requeue。
//   - 背压:某 model 触硬配额(池经 Ack.backpressure 上传)→ 该 model 的**新启动**
//     暂停至信号过期;在途不受影响、排队者不丢——池间内部流控(scheduling.md:90),
//     非请求级 shedding。

// errPreempted 是抢占取消的 cause,区别于客户端断开/超时(context.Canceled)——
// 前者重排,后者不重排。
var errPreempted = errors.New("preempted by higher priority request")

// errAbandoned 是客户端取消(在途时)的 cause:不重排、不投递——无人等待,
// 若被当抢占重排会产生无人等待的僵尸 Generate(review #56)。
var errAbandoned = errors.New("client cancelled in-flight request")

type pqRequest struct {
	priority int
	seq      uint64
	model    string
	exec     func(ctx context.Context) error
	done     chan error // buffered 1;Submit 返回后调度器写入不阻塞

	execCtx   context.Context // 执行 ctx(finishOne 读 cause 判抢占)
	cancel    context.CancelCauseFunc
	err       error // exec 返回值;执行 goroutine 写,经 finished chan 同步后读
	abandoned bool  // 客户端已在途取消(mu 保护):finishOne 不重排不投递
}

// PQScheduler 优先级调度器。Run 驱动执行;Submit 提交并等待结果。
type PQScheduler struct {
	maxInFlight int
	bpTTL       time.Duration // 背压信号有效期(心跳刷新;过期自动解除)

	mu       sync.Mutex
	queue    []*pqRequest // 等待中,已排序(priority desc, seq asc)
	inFlight map[*pqRequest]struct{}
	seq      uint64
	bp       map[string]time.Time // model → 背压截止时刻

	notify   chan struct{} // 队列/背压变化信号(buffered 1)
	finished chan *pqRequest
}

func NewPQScheduler(maxInFlight int, bpTTL time.Duration) *PQScheduler {
	if maxInFlight <= 0 {
		maxInFlight = 1
	}
	return &PQScheduler{
		maxInFlight: maxInFlight,
		bpTTL:       bpTTL,
		inFlight:    make(map[*pqRequest]struct{}),
		bp:          make(map[string]time.Time),
		notify:      make(chan struct{}, 1),
		finished:    make(chan *pqRequest),
	}
}

// Submit 提交一个已准入请求并等待执行完成。ctx 取消(客户端断开/超时):
// 排队中则摘除;在途则以 errAbandoned 取消 exec(不重排、不投递——无人等待)。
func (s *PQScheduler) Submit(
	ctx context.Context,
	priority int,
	model string,
	exec func(ctx context.Context) error,
) error {
	req := &pqRequest{
		priority: priority,
		model:    model,
		exec:     exec,
		done:     make(chan error, 1),
	}
	s.mu.Lock()
	s.seq++
	req.seq = s.seq
	s.insertLocked(req)
	s.mu.Unlock()
	s.maybePreempt(priority, model)
	s.signal()

	select {
	case err := <-req.done:
		return err
	case <-ctx.Done():
		s.mu.Lock()
		waiting := s.removeWaitingLocked(req)
		if !waiting && req.cancel != nil {
			// 在途(或刚完成):标记 abandoned 并取消执行;
			// 已完成的 cancel 是 no-op(cause 已定),abandoned 只是不再投递
			req.abandoned = true
			req.cancel(errAbandoned)
		}
		s.mu.Unlock()
		return ctx.Err()
	}
}

// SetMaxInFlight 动态调整并发上限(P6.5:随 ready 节点数伸缩)。
func (s *PQScheduler) SetMaxInFlight(n int) {
	if n <= 0 {
		n = 1
	}
	s.mu.Lock()
	s.maxInFlight = n
	s.mu.Unlock()
	s.signal()
}

// SetBackpressure 标记 model 背压(池触硬配额上传),now+ttl 后自动解除。
func (s *PQScheduler) SetBackpressure(model string, now time.Time) {
	s.mu.Lock()
	s.bp[model] = now.Add(s.bpTTL)
	s.mu.Unlock()
	s.signal()
}

// LoadSnapshot 调度器自身状态即 LoadReport(队列/in-flight/剩余容量)。
func (s *PQScheduler) LoadSnapshot(nodeID string) *lakepb.LoadReport {
	s.mu.Lock()
	defer s.mu.Unlock()
	inFlight := len(s.inFlight)
	remaining := s.maxInFlight - inFlight
	if remaining < 0 {
		remaining = 0
	}
	return &lakepb.LoadReport{
		NodeId:       nodeID,
		QueueLen:     uint32(len(s.queue)),
		InFlight:     uint32(inFlight),
		RemainingCap: uint32(remaining),
	}
}

// Run 调度循环:启动可执行的 → 等信号/完成/背压到期;收割完成件。ctx 取消即返回。
func (s *PQScheduler) Run(ctx context.Context) {
	var expiry <-chan time.Time
	var timer *time.Timer
	defer func() {
		if timer != nil {
			timer.Stop()
		}
	}()
	for {
		s.startEligible(ctx)
		// 背压暂停的最早到期时刻 → 定时唤醒重试(不丢请求,只是推迟启动)
		s.mu.Lock()
		var earliest time.Time
		now := time.Now()
		for _, r := range s.queue {
			if dl, ok := s.bp[r.model]; ok && now.Before(dl) {
				if earliest.IsZero() || dl.Before(earliest) {
					earliest = dl
				}
			}
		}
		s.mu.Unlock()
		if timer != nil {
			timer.Stop()
		}
		expiry = nil
		if !earliest.IsZero() {
			timer = time.NewTimer(time.Until(earliest))
			expiry = timer.C
		}

		select {
		case <-ctx.Done():
			return
		case <-s.notify:
		case req := <-s.finished:
			s.finishOne(req)
		case <-expiry:
		}
	}
}

func (s *PQScheduler) signal() {
	select {
	case s.notify <- struct{}{}:
	default:
	}
}

func (s *PQScheduler) insertLocked(req *pqRequest) {
	s.queue = append(s.queue, req)
	sort.SliceStable(s.queue, func(i, j int) bool {
		if s.queue[i].priority != s.queue[j].priority {
			return s.queue[i].priority > s.queue[j].priority
		}
		return s.queue[i].seq < s.queue[j].seq
	})
}

// startEligible 在容量内启动可执行的排队请求(跳过背压中的 model)。
func (s *PQScheduler) startEligible(ctx context.Context) {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now()
	for len(s.inFlight) < s.maxInFlight {
		idx := -1
		for i, r := range s.queue {
			if dl, ok := s.bp[r.model]; ok && now.Before(dl) {
				continue // 背压中:推迟启动,不丢
			}
			idx = i
			break
		}
		if idx < 0 {
			return
		}
		req := s.queue[idx]
		s.queue = append(s.queue[:idx], s.queue[idx+1:]...)
		s.inFlight[req] = struct{}{}
		req.execCtx, req.cancel = context.WithCancelCause(ctx)
		go func() {
			req.err = req.exec(req.execCtx)
			req.cancel(req.err) // 首个 cancel 定 cause;nil → context.Canceled(自然结束)
			s.finished <- req
		}()
	}
}

// finishOne 收割一个在途请求:abandoned(客户端取消)→ 不重排不投递;
// 被抢占且确实未执行完 → 重排回队列(原 seq,同优先级最前);
// 否则把 exec 结果写给 Submit 方。
//
// 竞态:抢占 cancel 与 exec 自然结束同时发生时,以 req.err 为准——err==nil 说明
// exec 已完整执行(取消没赶上),直接交付成功,绝不重排(避免同 request_id 二次执行)。
func (s *PQScheduler) finishOne(req *pqRequest) {
	s.mu.Lock()
	delete(s.inFlight, req)
	abandoned := req.abandoned
	s.mu.Unlock()

	if abandoned {
		return // 无人等待:不重排(防僵尸 Generate)、不投递
	}
	if req.err != nil && errors.Is(context.Cause(req.execCtx), errPreempted) {
		s.mu.Lock()
		s.insertLocked(req)
		s.mu.Unlock()
		s.signal()
		return
	}
	req.done <- req.err
}

// maybePreempt 在途已满且新请求 priority 严格高于在途最低者 → 抢占最低者
// (同优先级不抢占;最低并列时抢占 seq 最大=最晚开始者)。
// 新请求 model 处于背压时不抢占——它自己也启不来,踢掉在途只会空槽+无谓重算。
func (s *PQScheduler) maybePreempt(newPriority int, model string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.inFlight) < s.maxInFlight {
		return
	}
	if dl, ok := s.bp[model]; ok && time.Now().Before(dl) {
		return
	}
	var victim *pqRequest
	for r := range s.inFlight {
		if victim == nil || r.priority < victim.priority ||
			(r.priority == victim.priority && r.seq > victim.seq) {
			victim = r
		}
	}
	if victim != nil && newPriority > victim.priority && victim.cancel != nil {
		victim.cancel(errPreempted)
	}
}

// removeWaitingLocked 从等待队列摘除;返回 false = 不在队列(在途或已完成)。
func (s *PQScheduler) removeWaitingLocked(req *pqRequest) bool {
	for i, r := range s.queue {
		if r == req {
			s.queue = append(s.queue[:i], s.queue[i+1:]...)
			return true
		}
	}
	return false
}
