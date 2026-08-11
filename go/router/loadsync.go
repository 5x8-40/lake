package router

import (
	"context"
	"io"
	"log/slog"
	"time"

	lakepb "github.com/chengda-wu/lake/go/pb"
)

// RunLoadSync P6.4:Router → agent 的 ReportLoad 常驻循环(边10 上报通道):
// 周期性把调度器 LoadSnapshot(队列/in-flight/剩余容量)流式上报 agent;
// 读 ack 回收池写路径触硬配额的 BackpressureSignal,打进调度器(该 model
// 新启动暂停 bpTTL,不丢请求)。断流/错误延迟重连;仅 ctx 取消时返回。
//
// 信号语义对齐 docs/features/slo.md「过载控制」:推理系统的过载职责是**上报**
// 指标供 gateway 决策;Router 自身只据此做池间内部流控(暂停新启动),不拒请求。
// P7 对接 Bifrost 时本循环的 snapshot 即上报载荷。
func RunLoadSync(
	ctx context.Context,
	agent lakepb.AgentServiceClient,
	sched *PQScheduler,
	nodeID string,
	interval time.Duration,
) {
	for {
		if ctx.Err() != nil {
			return
		}
		runLoadSyncOnce(ctx, agent, sched, nodeID, interval)
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Second):
		}
	}
}

func runLoadSyncOnce(
	ctx context.Context,
	agent lakepb.AgentServiceClient,
	sched *PQScheduler,
	nodeID string,
	interval time.Duration,
) {
	stream, err := agent.ReportLoad(ctx)
	if err != nil {
		slog.Error("ReportLoad dial failed", "err", err)
		return
	}
	ackCh := make(chan *lakepb.Ack, 8)
	errCh := make(chan error, 1)
	go func() {
		for {
			ack, err := stream.Recv()
			if err != nil {
				errCh <- err
				return
			}
			select {
			case ackCh <- ack:
			default: // ack 是心跳语义,丢弃不损正确性(下一条 ack 会重带背压)
			}
		}
	}()
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case err := <-errCh:
			if err != io.EOF {
				slog.Error("ReportLoad recv failed", "err", err)
			}
			return
		case ack := <-ackCh:
			if bp := ack.GetBackpressure(); bp != nil {
				sched.SetBackpressure(bp.GetModelId(), time.Now())
			}
		case <-ticker.C:
			if err := stream.Send(sched.LoadSnapshot(nodeID)); err != nil {
				slog.Error("ReportLoad send failed", "err", err)
				return
			}
		}
	}
}
