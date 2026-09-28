//! Compiles `../proto/ramen/v1/*.proto` with protox (pure Rust, no protoc needed) into tonic stubs and a
//! descriptor set for server reflection (grpcurl without `-proto`).
use prost::Message;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let files = ["ramen/v1/mcp.proto", "ramen/v1/admin.proto"];
    for f in files {
        println!("cargo:rerun-if-changed=../proto/{f}");
    }
    let fds = protox::compile(files, ["../proto"])?;
    let out = std::path::PathBuf::from(std::env::var("OUT_DIR")?);
    std::fs::write(out.join("ramen_descriptor.bin"), fds.encode_to_vec())?;
    tonic_prost_build::configure()
        .build_client(true)
        .build_server(true)
        .compile_fds(fds)?;
    Ok(())
}
