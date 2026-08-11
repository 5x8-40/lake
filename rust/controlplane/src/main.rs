//! lake-controlplane:P3 内存权威 gRPC 服务。
//!
//! 默认监听 `0.0.0.0:50051`。环境变量 `LAKE_CP_ADDR` 可覆盖。

use lake_controlplane::ControlPlane;
use lake_proto::lake::control_plane_service_server::ControlPlaneServiceServer;
use tonic::transport::Server;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // 日志级别走 RUST_LOG(如 `RUST_LOG=debug`),缺省 info(显式 fallback——
    // `EnvFilter::from_default_env()` 在 RUST_LOG 未设时不含任何指令,不等于 info)。
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();
    let addr: std::net::SocketAddr = std::env::var("LAKE_CP_ADDR")
        .unwrap_or_else(|_| "0.0.0.0:50051".into())
        .parse()?;
    tracing::info!(%addr, "lake-controlplane listening");
    Server::builder()
        .add_service(ControlPlaneServiceServer::new(ControlPlane::default()))
        .serve(addr)
        .await?;
    Ok(())
}
