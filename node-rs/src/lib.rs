pub mod auth;
pub mod config;
pub mod grpc;
pub mod http;
pub mod log;
pub mod mcp;
pub mod metrics;
pub mod server;
pub mod session;
pub mod sidecar;
pub mod throttle;
pub mod token;

/// Generated from `proto/ramen/v1/*.proto` (see `build.rs`).
pub mod pb {
    tonic::include_proto!("ramen.v1");
    pub const FILE_DESCRIPTOR_SET: &[u8] =
        include_bytes!(concat!(env!("OUT_DIR"), "/ramen_descriptor.bin"));
}
