//! Flat content hashes → Dynamo `PositionalLineageHash` chain.
//!
//! 参考:`dynamo_tokens::PositionalLineageHash::{root,extend}`；
//! lake agent 传 `Vec<u8>` 扁平 hash，CP 用 xxh3_64 压成 `BlockHash=u64` 再建链。

use dynamo_tokens::{BlockHash, PositionalLineageHash};
use xxhash_rust::xxh3::xxh3_64;

/// Content-addressed flat bytes → 64-bit block hash (xxh3).
pub fn flat_to_block_hash(flat: &[u8]) -> BlockHash {
    xxh3_64(flat)
}

/// Build positional lineage for an ordered prefix of flat hashes.
///
/// `prefix_hashes[0]` → `root`; each next → `extend`.
pub fn lineage_from_prefix(prefix_hashes: &[Vec<u8>]) -> Vec<PositionalLineageHash> {
    let mut out = Vec::with_capacity(prefix_hashes.len());
    let mut prev: Option<PositionalLineageHash> = None;
    for flat in prefix_hashes {
        let bh = flat_to_block_hash(flat);
        let plh = match prev {
            None => PositionalLineageHash::root(bh),
            Some(p) => p.extend(bh),
        };
        prev = Some(plh);
        out.push(plh);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lineage_positions_monotonic() {
        let flats = vec![b"h0".to_vec(), b"h1".to_vec(), b"h2".to_vec()];
        let chain = lineage_from_prefix(&flats);
        assert_eq!(chain.len(), 3);
        assert_eq!(chain[0].position(), 0);
        assert_eq!(chain[1].position(), 1);
        assert_eq!(chain[2].position(), 2);
    }

    #[test]
    fn same_flat_same_block_hash() {
        assert_eq!(flat_to_block_hash(b"abc"), flat_to_block_hash(b"abc"));
        assert_ne!(flat_to_block_hash(b"abc"), flat_to_block_hash(b"abd"));
    }
}
