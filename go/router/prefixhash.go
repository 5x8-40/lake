package router

import (
	"crypto/sha256"
	"encoding/binary"
)

// BlockSize 与 python/lake/engine/agents/grpc_skeleton.py 的 BLOCK_SIZE 对齐
// (P3 mock=8;生产默认 128,届时走 ModelDescriptor 协商)。
const BlockSize = 8

// ChainBlockHashes 移植自 python grpc_skeleton.chain_block_hashes:
// 按 block_size 切 prompt,逐块 sha256(parent || tokens[4B LE]) 链式推进。
// 字节级必须与 Python 一致——worker/agent 侧注册用 Python 实现,Router 用本实现
// 查镜像,两端哈希不同则前缀命中恒 miss。
func ChainBlockHashes(tokenIDs []uint32, blockSize int) [][]byte {
	if blockSize <= 0 {
		blockSize = BlockSize
	}
	var hashes [][]byte
	var parent []byte
	for i := 0; i < len(tokenIDs); i += blockSize {
		end := i + blockSize
		if end > len(tokenIDs) {
			end = len(tokenIDs)
		}
		h := sha256.New()
		h.Write(parent) // nil 父 = 空串,对齐 Python b""
		var buf [4]byte
		for _, t := range tokenIDs[i:end] {
			binary.LittleEndian.PutUint32(buf[:], t)
			h.Write(buf[:])
		}
		digest := h.Sum(nil)
		hashes = append(hashes, digest)
		parent = digest
	}
	return hashes
}
