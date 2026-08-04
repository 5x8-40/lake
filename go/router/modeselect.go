package router

import (
	"context"

	lakepb "github.com/chengda-wu/lake/go/pb"
)

// P6.3:选路权威收归 Router——移植 python/lake/runtime/mode_select.py 的
// select_exec_mode 纯函数,Router 读本地命中视图镜像(P6.2 ViewMirror)零-RPC
// 决策 P/D/colocate/D-direct,经 GenerateRequest.exec_mode 下发 worker。
//
// 参考实现:
//   - python/lake/runtime/mode_select.py:18 select_exec_mode(被移植对象,逐分支对齐);
//   - vLLM SchedulerOutput(docs/research/scheduler-worker-interface.md):
//     调度侧决定、worker 消费——worker 不再持选路逻辑,与本文档的边界划分一致;
//   - Dynamo KV-aware router(docs/research/dynamo/overview.md):
//     router 侧按前缀 overlap 打分选 worker——lake 镜像 PrefixLookup 即其零-RPC 版。
//
// SLO 约束(docs/features/slo.md):D-direct 模式选择开销 < 5ms。
// 本实现为纯内存读(镜像 RWMutex 读锁 + map 查找),量级 µs,预算内。

// ExecMode 对齐 python/lake/runtime/exec_mode.py(线协议用字符串值)。
type ExecMode string

const (
	ExecModeColocated ExecMode = "COLOCATED" // 混部
	ExecModePDDisagg  ExecMode = "PD_DISAGG" // PD 分离
	ExecModeDDirect   ExecMode = "D_DIRECT"  // 本地命中直跳
)

// WorkerRole 对齐 python/lake/runtime/role.py(候选执行节点角色)。
type WorkerRole string

const (
	RolePrefill WorkerRole = "prefill"
	RoleDecode  WorkerRole = "decode"
	RoleHybrid  WorkerRole = "hybrid"
)

// PrefixHint 对齐 python/lake/runtime/prefix_hint.py(Router 侧构造,随请求下发)。
type PrefixHint struct {
	ComputedTokens int  // 已可复用 token 数(半开上界语义)
	ReusedBlocks   int  // 命中 block 数
	LocalHit       bool // 命中含 requester 本机 L0(含部分;D-direct 条件)
}

// SelectExecMode 移植 select_exec_mode:
//   - 本地命中(前缀已在本机 L0,含部分)→ D-direct,零/极小传输;
//   - 无本地命中 + 专用 Prefill/Decode 角色 → PD 分离;
//   - 否则混部。失败不设 mode-to-mode fallback(F4 重决策)。
func SelectExecMode(hint PrefixHint, promptLen int, role WorkerRole) ExecMode {
	if promptLen > 0 && hint.LocalHit && hint.ComputedTokens > 0 {
		return ExecModeDDirect
	}
	if role == RolePrefill || role == RoleDecode {
		return ExecModePDDisagg
	}
	return ExecModeColocated
}

// prefixHint 构造请求的前缀命中提示:先查本地镜像(零-RPC),
// 整段 miss 时回退 CP 权威查询(P6.1 LookupPrefixOnAuthority)。
//
// 为什么 miss 要回退:镜像只保证最终一致——注册事件(RegisterBlocks→广播)
// 尚未播到时的 total miss 是"假 miss",直接当冷请求处理会丢跨请求复用
// (smoke 判据:共享前缀的第二请求 reused_blocks>=3)。命中路径保持零-RPC;
// miss 确认的一次 RPC 相对整段重算可忽略。反向陈旧(镜像有、权威已逐出)
// 不查——worker miss 重算,只损性能不损正确性(docs/architecture/consistency.md §1)。
//
// computed_tokens 截断对齐 Python probe_prefix:min(hit*BLOCK_SIZE, prompt_len)。
func (s *Server) prefixHint(
	ctx context.Context,
	modelID string,
	prefixHashes [][]byte,
	promptLen int,
	requesterNodeID string,
) PrefixHint {
	if len(prefixHashes) == 0 || promptLen <= 0 {
		return PrefixHint{}
	}
	blocks, hitLen, _ := s.mirror.PrefixLookup(
		modelID, "", lakepb.PoolKind_TARGET, prefixHashes, requesterNodeID)
	if hitLen == 0 {
		// 镜像 total miss → 权威确认(冷启动/事件未播到/真 miss 三分支合一)。
		resp, err := s.LookupPrefixOnAuthority(ctx, &lakepb.LookupPrefixRequest{
			ModelId:         modelID,
			PrefixHashes:    prefixHashes,
			RequesterNodeId: requesterNodeID,
			PoolKind:        lakepb.PoolKind_TARGET,
		})
		if err != nil || resp == nil {
			return PrefixHint{} // 权威不可达按冷请求处理,不阻塞执行
		}
		return PrefixHint{
			ComputedTokens: minInt(int(resp.GetHitLength())*BlockSize, promptLen),
			ReusedBlocks:   int(resp.GetHitLength()),
			LocalHit:       anyLocalHit(resp.GetBlocks()),
		}
	}
	localHit := false
	for _, b := range blocks {
		if b.LocalHit {
			localHit = true
			break
		}
	}
	return PrefixHint{
		ComputedTokens: minInt(int(hitLen)*BlockSize, promptLen),
		ReusedBlocks:   int(hitLen),
		LocalHit:       localHit,
	}
}

// anyLocalHit:命中块任一在 requester 本机 L0(含部分命中;对齐 PrefixHint.local_hit 语义)。
func anyLocalHit(blocks []*lakepb.ReusableBlock) bool {
	for _, b := range blocks {
		if b.GetLocalHit() {
			return true
		}
	}
	return false
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
