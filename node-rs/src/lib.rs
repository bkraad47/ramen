pub mod activator;
pub mod auth;
pub mod config;
pub mod configs;
pub mod consent;
pub mod evals;
pub mod grpc;
pub mod guardrails;
pub mod http;
pub mod log;
pub mod mcp;
pub mod metrics;
pub mod modern;
pub mod netc;
pub mod otel;
pub mod revoke;
pub mod server;
pub mod session;
pub mod sidecar;
pub mod skillspector;
pub mod subs;
pub mod tasks;
pub mod telemetry;
#[cfg(test)]
pub(crate) mod testca;
pub mod throttle;
pub mod token;

/// Generated from `proto/ramen/v1/*.proto` (see `build.rs`).
pub mod pb {
    tonic::include_proto!("ramen.v1");
    pub const FILE_DESCRIPTOR_SET: &[u8] =
        include_bytes!(concat!(env!("OUT_DIR"), "/ramen_descriptor.bin"));
}
