package router

import (
	"errors"
	"testing"

	lakepb "github.com/chengda-wu/lake/go/pb"
)

func regEvent(model, hash string, locs ...*lakepb.Location) *lakepb.ViewEvent {
	return &lakepb.ViewEvent{
		Kind: lakepb.ViewEvent_REGISTERED,
		Id: &lakepb.KVBlockID{
			ModelId:   model,
			PoolKind:  lakepb.PoolKind_TARGET,
			BlockHash: []byte(hash),
		},
		Locations: locs,
		L3Present: true,
		BlockKind: lakepb.BlockKind_KIND_UNSPECIFIED,
	}
}

func l0on(node string) *lakepb.Location {
	return &lakepb.Location{Tier: lakepb.Tier_L0, NodeId: node, SegmentId: 1}
}

func TestMirrorApplySnapshotAnchorIncrement(t *testing.T) {
	m := NewViewMirror()
	// 快照 1:h0 + h1
	if err := m.Apply(&lakepb.ViewUpdate{Seq: 0, Events: []*lakepb.ViewEvent{
		regEvent("m", "h0"), regEvent("m", "h1", l0on("n0")),
	}}); err != nil {
		t.Fatal(err)
	}
	// 锚点:last → 3
	if err := m.Apply(&lakepb.ViewUpdate{Seq: 3}); err != nil {
		t.Fatal(err)
	}
	if got := m.LastSeq(); got != 3 {
		t.Fatalf("lastSeq = %d, want 3", got)
	}
	// 增量 4:MOVED h1 位置变更(全量携带,block_kind 保留)
	if err := m.Apply(&lakepb.ViewUpdate{Seq: 4, Events: []*lakepb.ViewEvent{{
		Kind: lakepb.ViewEvent_MOVED,
		Id: &lakepb.KVBlockID{
			ModelId: "m", PoolKind: lakepb.PoolKind_TARGET, BlockHash: []byte("h1"),
		},
		Locations: []*lakepb.Location{l0on("n1")},
	}}}); err != nil {
		t.Fatal(err)
	}
	blk, ok := m.Locate(&lakepb.KVBlockID{
		ModelId: "m", PoolKind: lakepb.PoolKind_TARGET, BlockHash: []byte("h1"),
	}, "n1")
	if !ok || !blk.LocalHit {
		t.Fatalf("h1 after MOVED: ok=%v local_hit=%v, want ok=true local_hit=true", ok, blk.LocalHit)
	}
	// 去重:seq<=last 的非空批静默丢弃
	if err := m.Apply(&lakepb.ViewUpdate{Seq: 2, Events: []*lakepb.ViewEvent{regEvent("m", "h9")}}); err != nil {
		t.Fatal(err)
	}
	if _, ok := m.Locate(&lakepb.KVBlockID{
		ModelId: "m", PoolKind: lakepb.PoolKind_TARGET, BlockHash: []byte("h9"),
	}, ""); ok {
		t.Fatal("dup update(seq<=last) must be dropped")
	}
	// 增量 5:INVALIDATED h0
	if err := m.Apply(&lakepb.ViewUpdate{Seq: 5, Events: []*lakepb.ViewEvent{{
		Kind: lakepb.ViewEvent_INVALIDATED,
		Id: &lakepb.KVBlockID{
			ModelId: "m", PoolKind: lakepb.PoolKind_TARGET, BlockHash: []byte("h0"),
		},
	}}}); err != nil {
		t.Fatal(err)
	}
	if _, ok := m.Locate(&lakepb.KVBlockID{
		ModelId: "m", PoolKind: lakepb.PoolKind_TARGET, BlockHash: []byte("h0"),
	}, ""); ok {
		t.Fatal("h0 must be invalidated")
	}
}

func TestMirrorApplyGap(t *testing.T) {
	m := NewViewMirror()
	if err := m.Apply(&lakepb.ViewUpdate{Seq: 0, Events: []*lakepb.ViewEvent{regEvent("m", "h0")}}); err != nil {
		t.Fatal(err)
	}
	if err := m.Apply(&lakepb.ViewUpdate{Seq: 1}); err != nil { // 锚点
		t.Fatal(err)
	}
	// 跳号:want 2 got 3 → gap,状态不变
	err := m.Apply(&lakepb.ViewUpdate{Seq: 3, Events: []*lakepb.ViewEvent{regEvent("m", "h1")}})
	var gap *ViewGapError
	if !errors.As(err, &gap) {
		t.Fatalf("err = %v, want *ViewGapError", err)
	}
	if gap.Want != 2 || gap.Got != 3 {
		t.Fatalf("gap = %+v, want {2 3}", gap)
	}
	if got := m.LastSeq(); got != 1 {
		t.Fatalf("lastSeq moved to %d on gap, want 1", got)
	}
	if _, ok := m.Locate(&lakepb.KVBlockID{
		ModelId: "m", PoolKind: lakepb.PoolKind_TARGET, BlockHash: []byte("h1"),
	}, ""); ok {
		t.Fatal("gap update must not be applied")
	}
}

func TestMirrorPrefixLookup(t *testing.T) {
	m := NewViewMirror()
	if err := m.Apply(&lakepb.ViewUpdate{Seq: 0, Events: []*lakepb.ViewEvent{
		regEvent("m", "h0", l0on("n0")),
		regEvent("m", "h1"), // 仅 L2/L3,无 L0
		regEvent("m", "h2", l0on("n1")),
	}}); err != nil {
		t.Fatal(err)
	}
	// 链上 h0..h2 全在镜像,但请求方 n0 只有 h0 local
	blocks, hitLen, allLocal := m.PrefixLookup("m", "", lakepb.PoolKind_TARGET,
		[][]byte{[]byte("h0"), []byte("h1"), []byte("h2")}, "n0")
	if hitLen != 3 {
		t.Fatalf("hitLen = %d, want 3", hitLen)
	}
	if allLocal {
		t.Fatal("allLocal = true, want false(仅 h0 在 n0)")
	}
	if !blocks[0].LocalHit || blocks[1].LocalHit || blocks[2].LocalHit {
		t.Fatalf("local_hit = [%v %v %v], want [true false false]",
			blocks[0].LocalHit, blocks[1].LocalHit, blocks[2].LocalHit)
	}
	// 连续语义:第一个 miss 即截断
	_, hitLen, _ = m.PrefixLookup("m", "", lakepb.PoolKind_TARGET,
		[][]byte{[]byte("hx"), []byte("h0")}, "n0")
	if hitLen != 0 {
		t.Fatalf("hitLen = %d, want 0(首块 miss 即截断)", hitLen)
	}
	// 命名空间隔离:DRAFT 池为空
	_, hitLen, _ = m.PrefixLookup("m", "", lakepb.PoolKind_DRAFT,
		[][]byte{[]byte("h0")}, "n0")
	if hitLen != 0 {
		t.Fatalf("DRAFT pool hitLen = %d, want 0(池域隔离)", hitLen)
	}
}
