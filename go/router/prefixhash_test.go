package router

import (
	"encoding/hex"
	"testing"
)

// 测试向量由 Python 权威实现生成(改哈希算法时须同步重生成):
//
//	PYTHONPATH=python python3 -c \
//	  "from lake.engine.agents.grpc_skeleton import chain_block_hashes; \
//	   [print(h.hex()) for h in chain_block_hashes(list(range(16)))]"
func TestChainBlockHashesCrossLangVector(t *testing.T) {
	tokens := make([]uint32, 16)
	for i := range tokens {
		tokens[i] = uint32(i)
	}
	got := ChainBlockHashes(tokens, BlockSize)
	want := []string{
		"ff1f6ee5d67458cfac950f62e93042e21fcb867e2234dcc8721801231064ad40",
		"db6e3872f1224a65b9d8238ba1f888935fa525fc0883a6d259369139d271ead9",
	}
	if len(got) != len(want) {
		t.Fatalf("len = %d, want %d", len(got), len(want))
	}
	for i, w := range want {
		if hex.EncodeToString(got[i]) != w {
			t.Fatalf("block %d = %x, want %s", i, got[i], w)
		}
	}
}

// 尾块不满一个 block 也要哈希(与 Python 一致:chunk 非空即算)。
func TestChainBlockHashesPartialTailBlock(t *testing.T) {
	got := ChainBlockHashes([]uint32{7}, BlockSize)
	if len(got) != 1 {
		t.Fatalf("len = %d, want 1", len(got))
	}
	const want = "e8613f5a5bc9f9feeda32a8e7c80b69dd4878e47b6a91723fb15eb84236b6a2b"
	if hex.EncodeToString(got[0]) != want {
		t.Fatalf("= %x, want %s", got[0], want)
	}
	if got := ChainBlockHashes(nil, BlockSize); len(got) != 0 {
		t.Fatalf("empty prompt: len = %d, want 0", len(got))
	}
}
