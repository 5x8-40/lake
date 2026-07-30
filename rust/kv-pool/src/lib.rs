//! KV Pool:`TcpDataService` 内存字节存储（dumb 后端）。
//!
//! P4.2:索引 / radix / ref 归 controlplane；本 crate 只按
//! `(model_id, revision, pool_kind, block_hash) → bytes` 存取，无 lookup 职责。
//! P4.4:正名自 SkeletonKv；对齐 Mooncake `MC_FORCE_TCP` fallback——
//! 无 RDMA 时 gRPC 传不透明 bytes；生产旁路走 TransferService + Transport。
//! 参考:LMCache MemoryObj；Mooncake store Put/Get；`TcpTransport`。

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use tonic::{Request, Response, Status};

pub use lake_proto::lake::*;

use tcp_data_service_server::TcpDataService;

#[derive(Default)]
struct Store {
    /// key = (model_id, revision, pool_kind, block_hash)
    data: HashMap<(String, String, i32, Vec<u8>), Vec<u8>>,
}

fn key(id: &KvBlockId) -> (String, String, i32, Vec<u8>) {
    (
        id.model_id.clone(),
        id.revision.clone(),
        id.pool_kind,
        id.block_hash.clone(),
    )
}

#[derive(Clone, Default)]
pub struct KvPool {
    inner: Arc<Mutex<Store>>,
}

#[tonic::async_trait]
impl TcpDataService for KvPool {
    async fn put_blocks(
        &self,
        request: Request<PutBlocksRequest>,
    ) -> Result<Response<Ack>, Status> {
        let req = request.into_inner();
        let mut store = self.inner.lock().unwrap();
        let mut skipped_missing_id = 0usize;
        for blk in req.blocks {
            let Some(id) = blk.id else {
                skipped_missing_id += 1;
                continue;
            };
            store.data.insert(key(&id), blk.data);
        }
        Ok(Response::new(Ack {
            ok: true,
            err: if skipped_missing_id == 0 {
                String::new()
            } else {
                format!("skipped {skipped_missing_id} block(s) without id")
            },
            backpressure: None,
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
