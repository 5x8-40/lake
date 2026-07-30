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
            ok: skipped_missing_id == 0,
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

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn put_blocks_missing_id_is_not_ok() {
        let pool = KvPool::default();
        let ack = pool
            .put_blocks(Request::new(PutBlocksRequest {
                node_id: "n0".into(),
                blocks: vec![OpaqueBlock {
                    id: None,
                    data: b"bad".to_vec(),
                }],
            }))
            .await
            .unwrap()
            .into_inner();

        assert!(!ack.ok);
        assert!(ack.err.contains("skipped 1 block"));
    }

    #[tokio::test]
    async fn put_then_get_blocks_round_trip() {
        let pool = KvPool::default();
        let id = KvBlockId {
            model_id: "m".into(),
            revision: "r1".into(),
            pool_kind: PoolKind::Target as i32,
            block_hash: b"h0".to_vec(),
            scope: "public".into(),
        };
        let data = b"payload".to_vec();

        let ack = pool
            .put_blocks(Request::new(PutBlocksRequest {
                node_id: "n0".into(),
                blocks: vec![OpaqueBlock {
                    id: Some(id.clone()),
                    data: data.clone(),
                }],
            }))
            .await
            .unwrap()
            .into_inner();
        assert!(ack.ok, "put failed: {}", ack.err);

        let resp = pool
            .get_blocks(Request::new(GetBlocksRequest {
                ids: vec![id.clone()],
            }))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(resp.blocks.len(), 1);
        assert_eq!(resp.blocks[0].id.as_ref(), Some(&id));
        assert_eq!(resp.blocks[0].data, data);
    }
}
