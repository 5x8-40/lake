//! KV Pool:SkeletonKv 内存字节存储（dumb 后端）。
//!
//! P4.2:索引 / radix / ref 归 controlplane；本 crate 只按
//! `(model_id, pool_kind, block_hash) → bytes` 存取，无 lookup 职责。
//! 生产字节走 RDMA；此处 gRPC 传不透明 bytes。
//! 参考:LMCache MemoryObj；Mooncake store Put/Get。

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use tonic::{Request, Response, Status};

pub use lake_proto::lake::*;

use skeleton_kv_service_server::SkeletonKvService;

#[derive(Default)]
struct Store {
    /// key = (model_id, pool_kind, block_hash)
    data: HashMap<(String, i32, Vec<u8>), Vec<u8>>,
}

fn key(id: &KvBlockId) -> (String, i32, Vec<u8>) {
    (id.model_id.clone(), id.pool_kind, id.block_hash.clone())
}

#[derive(Clone, Default)]
pub struct KvPool {
    inner: Arc<Mutex<Store>>,
}

#[tonic::async_trait]
impl SkeletonKvService for KvPool {
    async fn put_blocks(
        &self,
        request: Request<PutBlocksRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let mut store = self.inner.lock().unwrap();
        for blk in req.blocks {
            let Some(id) = blk.id else { continue };
            store.data.insert(key(&id), blk.data);
        }
        Ok(Response::new(Ack {
            ok: true,
            err: String::new(),
        }))
    }

    async fn get_blocks(
        &self,
        request: Request<GetBlocksRequest>,
    ) -> Result<Response<GetBlocksResponse>, Status> {
        let req = request.into_inner();
        let store = self.inner.lock().unwrap();
        let mut blocks = Vec::new();
        for id in req.ids {
            if let Some(data) = store.data.get(&key(&id)) {
                blocks.push(OpaqueBlock {
                    id: Some(id),
                    data: data.clone(),
                });
            }
        }
        Ok(Response::new(GetBlocksResponse { blocks }))
    }
}
