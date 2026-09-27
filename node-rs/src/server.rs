//! Startup: initial `runtime.load`, serve until `shutdown` resolves, then stop the sidecar.
use crate::config::Config;
use crate::http::{self, Shared};
use crate::log::emit;
use serde_json::json;
use std::net::SocketAddr;
use tokio::net::TcpListener;

pub async fn run(
    cfg: Config,
    listener: TcpListener,
    shutdown: impl Future<Output = ()> + Send + 'static,
) -> Shared {
    emit(
        "info",
        "ramen-node start",
        json!({"version": env!("CARGO_PKG_VERSION"), "addr": listener.local_addr().ok(), "bucket": cfg.bucket, "group": cfg.group, "zone": cfg.zone, "keys": cfg.mcp_keys.len()}),
    );
    crate::log::set_file(cfg.log_file.clone());
    let app = http::app(cfg.clone());
    app.sidecar.start_reaper();
    if cfg.bucket.join("mcp").is_dir() {
        if let Err(e) = app.sidecar.load().await {
            emit("warn", "initial load failed", json!({"error": e.message}));
        }
    } else {
        emit(
            "warn",
            "bucket has no mcp/ yet; waiting for /admin/reload",
            json!({"bucket": cfg.bucket}),
        );
    }
    let svc = http::router(app.clone()).into_make_service_with_connect_info::<SocketAddr>();
    if let Err(e) = axum::serve(listener, svc)
        .with_graceful_shutdown(shutdown)
        .await
    {
        emit("error", "serve failed", json!({"error": e.to_string()}));
    }
    app.sidecar.kill("shutdown").await;
    app
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[tokio::test]
    async fn serves_healthz_then_shuts_down() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let m: HashMap<_, _> = [(
            "RAMEN_BUCKET".to_string(),
            std::env::temp_dir()
                .join("ramen-nope")
                .display()
                .to_string(),
        )]
        .into();
        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        let server = tokio::spawn(run(Config::from_map(&m).unwrap(), listener, async move {
            let _ = rx.await;
        }));
        let mut s = tokio::net::TcpStream::connect(addr).await.unwrap();
        s.write_all(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            .await
            .unwrap();
        let mut buf = String::new();
        s.read_to_string(&mut buf).await.unwrap();
        assert!(buf.starts_with("HTTP/1.1 200") && buf.ends_with("ok"));
        tx.send(()).unwrap();
        let app = server.await.unwrap();
        assert!(!app.sidecar.alive().await && app.sidecar.loaded().await.is_none());
    }
}
