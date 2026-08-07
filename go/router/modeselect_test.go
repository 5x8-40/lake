package router

import (
	"context"
	"testing"

	lakepb "github.com/chengda-wu/lake/go/pb"
)

// 判据(issue #52 P6.3):三模式选路单测覆盖。
// 与 python/lake/runtime/tests/test_prebuilt.py 的 select_exec_mode 用例逐一对齐。
// P7.6(issue #68 条目 4):SelectExecMode 第四参 bwGBps;0 = 未配置跳过带宽闸
// (既有用例维持 P6.3 语义),>0 启用「传 vs 算」带宽闸。

// Pool 命中(非本地)→ PD 传:命中在分布式池需传输,专用角色走 PD 分离。
func TestSelectExecModePoolHitPD(t *testing.T) {
	h := PrefixHint{ComputedTokens: 16, ReusedBlocks: 2, LocalHit: false}
	if got := SelectExecMode(h, 16, RolePrefill, 0); got != ExecModePDDisagg {
		t.Fatalf("prefill role = %s, want PD_DISAGG", got)
	}
	if got := SelectExecMode(h, 16, RoleDecode, 0); got != ExecModePDDisagg {
		t.Fatalf("decode role = %s, want PD_DISAGG", got)
	}
	// 带宽 ≥ 阈值:池命中仍 PD(传划算)
	if got := SelectExecMode(h, 16, RolePrefill, 10); got != ExecModePDDisagg {
		t.Fatalf("prefill role + bw 10 = %s, want PD_DISAGG", got)
	}
}

// 带宽闸(issue #68 条目 4):池命中 + 路径带宽 < 阈值 → 混部重算(传不划算)。
func TestSelectExecModeBandwidthGate(t *testing.T) {
	h := PrefixHint{ComputedTokens: 16, ReusedBlocks: 2, LocalHit: false}
	if got := SelectExecMode(h, 16, RolePrefill, 0.5); got != ExecModeColocated {
		t.Fatalf("pool hit + bw 0.5 = %s, want COLOCATED(重算便宜)", got)
	}
	if got := SelectExecMode(h, 16, RoleDecode, 0.99); got != ExecModeColocated {
		t.Fatalf("pool hit + bw 0.99 = %s, want COLOCATED", got)
	}
	// 本地命中优先于带宽闸:块在本机 L0,无传输需求,低带宽也 D-direct
	local := PrefixHint{ComputedTokens: 8, ReusedBlocks: 1, LocalHit: true}
	if got := SelectExecMode(local, 16, RoleHybrid, 0.3); got != ExecModeDDirect {
		t.Fatalf("local hit + bw 0.3 = %s, want D_DIRECT", got)
	}
	// 无命中不受带宽闸影响:没有可传的 KV,角色决策照旧
	if got := SelectExecMode(PrefixHint{}, 16, RolePrefill, 0.3); got != ExecModePDDisagg {
		t.Fatalf("miss + bw 0.3 = %s, want PD_DISAGG(无可传)", got)
	}
}

// 本地命中 → D-direct 零传:前缀已在执行节点 HBM(含部分),任何角色都直跳。
func TestSelectExecModeLocalHitDDirect(t *testing.T) {
	h := PrefixHint{ComputedTokens: 8, ReusedBlocks: 1, LocalHit: true}
	for _, role := range []WorkerRole{RoleHybrid, RolePrefill, RoleDecode} {
		if got := SelectExecMode(h, 16, role, 0); got != ExecModeDDirect {
			t.Fatalf("role %s = %s, want D_DIRECT", role, got)
		}
	}
}

// 混部:无本地命中 + HYBRID(同节点完成 P+D)。
func TestSelectExecModeColocated(t *testing.T) {
	if got := SelectExecMode(PrefixHint{}, 8, RoleHybrid, 0); got != ExecModeColocated {
		t.Fatalf("miss + hybrid = %s, want COLOCATED", got)
	}
	// Pool 命中但非本地 + HYBRID → 仍混部(传输由 pool 拉取,不拆 PD)
	h := PrefixHint{ComputedTokens: 8, ReusedBlocks: 1, LocalHit: false}
	if got := SelectExecMode(h, 16, RoleHybrid, 0); got != ExecModeColocated {
		t.Fatalf("pool hit + hybrid = %s, want COLOCATED", got)
	}
	// 边界:空 prompt / 零 computed 不触发 D-direct
	if got := SelectExecMode(PrefixHint{LocalHit: true}, 0, RoleHybrid, 0); got != ExecModeColocated {
		t.Fatalf("empty prompt = %s, want COLOCATED", got)
	}
}

// prefixHint:镜像命中零-RPC(local_hit 逐块语义,部分命中也算)。
func TestPrefixHintMirrorHit(t *testing.T) {
	srv := dialFakeCP(t, fakeCP{}) // CP 不应被调到(命中路径零 RPC)
	m := srv.Mirror()
	hashes := ChainBlockHashes([]uint32{1, 2, 3, 4, 5, 6, 7, 8}, BlockSize)
	if err := m.Apply(&lakepb.ViewUpdate{Seq: 0, Events: []*lakepb.ViewEvent{
		regEvent("m", string(hashes[0]), l0on("worker-0")),
	}}); err != nil {
		t.Fatal(err)
	}
	hint := srv.prefixHint(context.Background(), "m", hashes, 8, "worker-0")
	if hint.ReusedBlocks != 1 || hint.ComputedTokens != 8 || !hint.LocalHit {
		t.Fatalf("hint = %+v, want {8 1 local}", hint)
	}
	// 换 requester → 同一块非本地 → local_hit=false(池命中)
	hint = srv.prefixHint(context.Background(), "m", hashes, 8, "worker-1")
	if hint.ReusedBlocks != 1 || hint.LocalHit {
		t.Fatalf("hint = %+v, want {8 1 remote}", hint)
	}
}

// prefixHint:镜像 total miss → 回退权威(事件未播到/冷启动);权威也 miss → 冷请求。
func TestPrefixHintMirrorMissFallsBackToAuthority(t *testing.T) {
	called := 0
	srv := dialFakeCP(t, fakeCP{
		lookupPrefix: func(ctx context.Context, req *lakepb.LookupPrefixRequest) (*lakepb.LookupPrefixResponse, error) {
			called++
			return &lakepb.LookupPrefixResponse{
				HitLength: 1,
				Blocks: []*lakepb.ReusableBlock{{
					Id: &lakepb.KVBlockID{ModelId: req.GetModelId(), BlockHash: req.GetPrefixHashes()[0]},
				}},
			}, nil
		},
	})
	hashes := ChainBlockHashes([]uint32{9, 9, 9, 9, 9, 9, 9, 9}, BlockSize)
	hint := srv.prefixHint(context.Background(), "m", hashes, 8, "worker-0")
	if called != 1 {
		t.Fatalf("authority called %d times, want 1(mirror miss fallback)", called)
	}
	if hint.ReusedBlocks != 1 || hint.ComputedTokens != 8 || hint.LocalHit {
		t.Fatalf("hint = %+v, want {8 1 remote}", hint)
	}

	// 权威也 miss → 零 hint(冷请求,按 COLOCATED 无复用处理)
	srv2 := dialFakeCP(t, fakeCP{
		lookupPrefix: func(ctx context.Context, req *lakepb.LookupPrefixRequest) (*lakepb.LookupPrefixResponse, error) {
			return &lakepb.LookupPrefixResponse{}, nil
		},
	})
	hint = srv2.prefixHint(context.Background(), "m", hashes, 8, "worker-0")
	if hint != (PrefixHint{}) {
		t.Fatalf("hint = %+v, want zero", hint)
	}
}
