//! 内容寻址字节后端(Pull / TcpData 共用)。
//!
//! 对齐 kv-pool dumb store:`(model_id, revision, pool_kind, block_hash) → bytes`。
//! TransferService.Pull 从这里取源字节,再经 TcpTransport 写入段。

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use lake_proto::lake::KvBlockId;

type BlockKey = (String, String, i32, Vec<u8>);
type StoreMap = HashMap<BlockKey, Vec<u8>>;

fn key(id: &KvBlockId) -> BlockKey {
    (
        id.model_id.clone(),
        id.revision.clone(),
        id.pool_kind,
        id.block_hash.clone(),
    )
}

/// 进程内内容寻址 KV 字节(TCP 数据面 / Pull 源)。
#[derive(Clone, Default)]
pub struct InMemoryByteStore {
    inner: Arc<Mutex<StoreMap>>,
}

impl InMemoryByteStore {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn put(&self, id: &KvBlockId, data: Vec<u8>) {
        self.inner.lock().unwrap().insert(key(id), data);
    }

    pub fn get(&self, id: &KvBlockId) -> Option<Vec<u8>> {
        self.inner.lock().unwrap().get(&key(id)).cloned()
    }

    pub fn contains(&self, id: &KvBlockId) -> bool {
        self.inner.lock().unwrap().contains_key(&key(id))
    }

    pub fn len(&self) -> usize {
        self.inner.lock().unwrap().len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}
