package router

import (
	"fmt"
	"sync"

	lakepb "github.com/chengda-wu/lake/go/pb"
)

// ViewMirror 是 CP 权威位置视图的只读镜像(P6.2):消费 SubscribeView 的
// snapshot + 增量 ViewEvent,apply 后服务本地零-RPC 查询。
//
// 协议语义(见 rust/controlplane/src/view.rs 头注释):
//   - seq=0 → 快照:重置镜像后按事件重建;
//   - 空事件 → 锚点:last 前进到权威 seq(快照/重放之后);
//   - 非空增量要求 seq == last+1,否则返回 *ViewGapError(调用方 resume 重连);
//   - seq <= last 的非空批是去重对象(快照与广播重叠),静默丢弃。
//
// 参考:Dynamo `DeduplicatingStream` 的 (publisher_id, sequence) 去重——
// lake 单权威退化为单流序号(control-plane.md:122)。
type ViewMirror struct {
	mu      sync.RWMutex
	lastSeq uint64
	blocks  map[mirrorNSKey]map[string]*mirrorEntry
}

type mirrorNSKey struct {
	modelID  string
	revision string
	poolKind lakepb.PoolKind
}

type mirrorEntry struct {
	locations []*lakepb.Location
	l3Present bool
	blockKind lakepb.BlockKind
}

func NewViewMirror() *ViewMirror {
	return &ViewMirror{blocks: make(map[mirrorNSKey]map[string]*mirrorEntry)}
}

// LastSeq 镜像已对齐的权威 seq(0 = 未同步)。
func (m *ViewMirror) LastSeq() uint64 {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.lastSeq
}

// ViewGapError:增量 seq 跳号——调用方应以 resume_from_seq=LastSeq() 重连。
type ViewGapError struct {
	Want, Got uint64
}

func (e *ViewGapError) Error() string {
	return fmt.Sprintf("view update gap: want seq %d, got %d", e.Want, e.Got)
}

// Apply 应用一条 ViewUpdate(语义见类型注释)。
func (m *ViewMirror) Apply(u *lakepb.ViewUpdate) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if u.GetSeq() == 0 {
		// 快照:重置后重建(空快照同样是合法重置)。
		m.blocks = make(map[mirrorNSKey]map[string]*mirrorEntry)
		for _, ev := range u.GetEvents() {
			m.upsertLocked(ev)
		}
		return nil
	}
	if len(u.GetEvents()) == 0 {
		// 锚点:快照/重放末尾同步权威 seq。
		if u.GetSeq() > m.lastSeq {
			m.lastSeq = u.GetSeq()
		}
		return nil
	}
	if u.GetSeq() <= m.lastSeq {
		return nil // 去重:快照/广播重叠或重放重复
	}
	if u.GetSeq() != m.lastSeq+1 {
		return &ViewGapError{Want: m.lastSeq + 1, Got: u.GetSeq()}
	}
	m.lastSeq = u.GetSeq()
	for _, ev := range u.GetEvents() {
		if ev.GetKind() == lakepb.ViewEvent_INVALIDATED {
			m.deleteLocked(ev.GetId())
		} else {
			// REGISTERED / MOVED 均携带变更后全量位置 → upsert。
			m.upsertLocked(ev)
		}
	}
	return nil
}

func mirrorKeyOf(id *lakepb.KVBlockID) mirrorNSKey {
	return mirrorNSKey{
		modelID:  id.GetModelId(),
		revision: id.GetRevision(),
		poolKind: id.GetPoolKind(),
	}
}

func (m *ViewMirror) upsertLocked(ev *lakepb.ViewEvent) {
	id := ev.GetId()
	if id == nil {
		return
	}
	k := mirrorKeyOf(id)
	pool := m.blocks[k]
	if pool == nil {
		pool = make(map[string]*mirrorEntry)
		m.blocks[k] = pool
	}
	e := pool[string(id.GetBlockHash())]
	if e == nil {
		e = &mirrorEntry{}
		pool[string(id.GetBlockHash())] = e
	}
	e.locations = ev.GetLocations()
	e.l3Present = ev.GetL3Present()
	// block_kind 仅 REGISTERED 首次携带(proto 注释);MOVED 保留旧值。
	if ev.GetKind() == lakepb.ViewEvent_REGISTERED {
		e.blockKind = ev.GetBlockKind()
	}
}

func (m *ViewMirror) deleteLocked(id *lakepb.KVBlockID) {
	if id == nil {
		return
	}
	k := mirrorKeyOf(id)
	if pool := m.blocks[k]; pool != nil {
		delete(pool, string(id.GetBlockHash()))
		if len(pool) == 0 {
			delete(m.blocks, k)
		}
	}
}

// MirrorBlock 镜像条目的只读快照(供选路读)。
type MirrorBlock struct {
	Locations []*lakepb.Location
	L3Present bool
	BlockKind lakepb.BlockKind
	// LocalHit = 持有 requester 节点的 L0 位置(D-direct 信号,对齐 Rust
	// `ReusableBlock.local_hit` = L0-on-requester)。
	LocalHit bool
}

// Locate 按 block id 查镜像条目(零 RPC)。
func (m *ViewMirror) Locate(id *lakepb.KVBlockID, requesterNodeID string) (MirrorBlock, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	pool := m.blocks[mirrorKeyOf(id)]
	e := pool[string(id.GetBlockHash())]
	if e == nil {
		return MirrorBlock{}, false
	}
	return MirrorBlock{
		Locations: e.locations,
		L3Present: e.l3Present,
		BlockKind: e.blockKind,
		LocalHit:  hasL0On(e.locations, requesterNodeID),
	}, true
}

// PrefixLookup 沿 prefix_hashes 链查连续命中(零 RPC 版 lookup_prefix 镜像)。
//
// 与 Rust `Authority::lookup_prefix` 的关键差异:ViewEvent 不带 prefix_chain,
// 镜像无法做 lineage 校验——同一 flat 出现在两条不同链的极端冲突会误报命中。
// 误报/陈旧只损性能(worker miss 重算、多一跳回退),不损正确性
// (docs/architecture/consistency.md §1);权威校验留 LookupPrefixOnAuthority 回退档。
// 若 P6.3 需要严格 lineage,需给 ViewEvent 增 prefix_chain 字段(proto 变更)。
func (m *ViewMirror) PrefixLookup(
	modelID, revision string,
	poolKind lakepb.PoolKind,
	prefixHashes [][]byte,
	requesterNodeID string,
) (blocks []MirrorBlock, hitLength uint32, allLocal bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	pool := m.blocks[mirrorNSKey{modelID: modelID, revision: revision, poolKind: poolKind}]
	allLocal = true
	for _, h := range prefixHashes {
		e := pool[string(h)]
		if e == nil {
			allLocal = false
			break
		}
		localHit := hasL0On(e.locations, requesterNodeID)
		blocks = append(blocks, MirrorBlock{
			Locations: e.locations,
			L3Present: e.l3Present,
			BlockKind: e.blockKind,
			LocalHit:  localHit,
		})
		hitLength++
		// all_local_hit = 全链命中 且 每块都在 requester 本地(对齐 Rust 语义)
		allLocal = allLocal && localHit
	}
	if hitLength == 0 {
		allLocal = false
	}
	return blocks, hitLength, allLocal
}

func hasL0On(locs []*lakepb.Location, nodeID string) bool {
	for _, l := range locs {
		if l.GetTier() == lakepb.Tier_L0 && l.GetNodeId() == nodeID {
			return true
		}
	}
	return false
}
