// Package router 是请求控制面空壳(无状态路由 + 模式选择)。
//
// P2:仅验证能引用 lakepb 生成类型编译通过;无业务逻辑。
// 集群级调度(池间/弹性/优先级排队)归本包同进程逻辑,不拆独立 scheduler 进程
// (见 docs/architecture/control-plane.md、#3)。
//
// 参考:Dynamo LocalScheduler 内嵌 kv-router(请求级选路,非 engine batch scheduler)。
package router

import (
	lakepb "github.com/chengda-wu/lake/go/pb"
)

// Compile-time 锚定:保证生成的 Dispatch / LookupPrefix 消息仍在 stub 中。
var (
	_ = (*lakepb.DispatchRequest)(nil)
	_ = (*lakepb.LookupPrefixRequest)(nil)
	_ = (*lakepb.SubscribeRequest)(nil)
	_ = lakepb.PullPolicy_PULL_BEST_EFFORT
)
