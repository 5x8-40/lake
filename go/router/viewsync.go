package router

import (
	"context"
	"errors"
	"io"
	"time"

	lakepb "github.com/chengda-wu/lake/go/pb"
)

// RunViewSync 消费 CP SubscribeView stream 维护本地镜像(P6.2),常驻监督循环:
// gap → 立即以 resume_from_seq=LastSeq() 重连(走 CP replay buffer);
// 断线/CP 正常关闭 → reconnectDelay 后重连;resume 点过老时 CP 自动回退快照(镜像重置)。
// 仅当 ctx 取消时返回。
func RunViewSync(
	ctx context.Context,
	cp lakepb.ControlPlaneServiceClient,
	mirror *ViewMirror,
	subscriberID string,
	reconnectDelay time.Duration,
) error {
	for {
		err := viewSyncOnce(ctx, cp, mirror, subscriberID)
		if ctx.Err() != nil {
			return nil
		}
		var gap *ViewGapError
		if err != nil && errors.As(err, &gap) {
			continue // gap:立即 resume 重连
		}
		// io.EOF(CP 正常关流)与其他错误一样,延迟重连保持镜像新鲜。
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(reconnectDelay):
		}
	}
}

func viewSyncOnce(
	ctx context.Context,
	cp lakepb.ControlPlaneServiceClient,
	mirror *ViewMirror,
	subscriberID string,
) error {
	stream, err := cp.SubscribeView(ctx, &lakepb.SubscribeRequest{
		SubscriberId:  subscriberID,
		ResumeFromSeq: mirror.LastSeq(),
	})
	if err != nil {
		return err
	}
	for {
		u, err := stream.Recv()
		if err == io.EOF {
			return nil // CP 正常关流
		}
		if err != nil {
			return err
		}
		if err := mirror.Apply(u); err != nil {
			return err
		}
	}
}
