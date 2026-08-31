//! lake-storage-agent:AgentService(Router→agent 调度) + TransferService(节点间传输)。
//!
//! 默认 `0.0.0.0:50054`。环境变量 `LAKE_AGENT_ADDR` 可覆盖。
//! Transfer 用进程内 `TcpTransport` + `InMemoryByteStore`(单测/冒烟站位;
//! 与 kv-pool `TcpDataService` 跨进程共享字节 → 后续接 gRPC 客户端)。

use std::sync::Arc;

use lake_proto::lake::agent_service_server::AgentServiceServer;
use lake_proto::lake::transfer_service_server::TransferServiceServer;
use lake_storage_agent::Agent;
use lake_transfer::{InMemoryByteStore, TcpTransport, TransferServer};
use tonic::transport::Server;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let addr: std::net::SocketAddr = std::env::var("LAKE_AGENT_ADDR")
        .unwrap_or_else(|_| "0.0.0.0:50054".into())
        .parse()?;
    let transfer = TransferServer::new(
        Arc::new(TcpTransport::new()),
        Arc::new(InMemoryByteStore::new()),
    );
    println!("lake-storage-agent (AgentService+TransferService) listening on {addr}");
    Server::builder()
        .add_service(AgentServiceServer::new(Agent))
        .add_service(TransferServiceServer::new(transfer))
        .serve(addr)
        .await?;
    Ok(())
}
