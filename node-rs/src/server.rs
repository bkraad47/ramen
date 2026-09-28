//! Startup: initial `runtime.load` (when mcp/ exists or `RAMEN_BUCKET_URI` is set), serve gRPC (h2c, or TLS with
//! `RAMEN_TLS_CERT`/`RAMEN_TLS_KEY`) until `shutdown` resolves, then stop the sidecar.
use crate::config::Config;
use crate::grpc::{self, Shared};
use crate::log::emit;
use serde_json::json;
use tokio::net::TcpListener;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::transport::{Identity, Server, ServerTlsConfig};

pub async fn run(
    cfg: Config,
    listener: TcpListener,
    shutdown: impl Future<Output = ()> + Send + 'static,
) -> Shared {
    emit(
        "info",
        "ramen-node start",
        json!({"version": env!("CARGO_PKG_VERSION"), "addr": listener.local_addr().ok(), "bucket": cfg.bucket, "group": cfg.group, "zone": cfg.zone, "keys": cfg.mcp_keys.len(), "tls": cfg.tls.is_some()}),
    );
    crate::log::set_file(cfg.log_file.clone());
    let app = grpc::app(cfg.clone()).await;
    app.sidecar.start_reaper();
    // A bucket URI means the runtime fills the dir itself on load (§7), so try even when mcp/ is missing.
    if cfg.bucket.join("mcp").is_dir() || cfg.bucket_uri.is_some() {
        if let Err(e) = app.load().await {
            emit("warn", "initial load failed", json!({"error": e.message}));
            retry_initial_load(app.clone(), cfg.load_retry_secs);
        }
    } else {
        emit(
            "warn",
            "bucket has no mcp/ yet; waiting for Admin/Reload",
            json!({"bucket": cfg.bucket}),
        );
    }
    let builder = match tls(&cfg) {
        Ok(Some(t)) => Server::builder().tls_config(t).map_err(|e| e.to_string()),
        Ok(None) => Ok(Server::builder()),
        Err(e) => Err(e),
    };
    match builder {
        Ok(mut b) => {
            if let Err(e) = b
                .add_routes(grpc::routes(&app))
                .serve_with_incoming_shutdown(TcpListenerStream::new(listener), shutdown)
                .await
            {
                emit("error", "serve failed", json!({"error": e.to_string()}));
            }
        }
        Err(e) => emit("error", "tls config failed", json!({"error": e})),
    }
    app.sidecar.kill("shutdown").await;
    app
}

/// Keep retrying the initial `runtime.load` (backoff ×2, capped at 60 s) until something is loaded. Transient failures
/// (Workload Identity / IAM propagation right after a zone is attached, bucket still syncing) must not leave the pod
/// NOT_SERVING for ever: the console only sends `Admin/Reload` once the pod is ready.
fn retry_initial_load(app: Shared, first_delay_secs: u64) {
    if first_delay_secs == 0 {
        return;
    }
    tokio::spawn(async move {
        let mut delay = first_delay_secs;
        let mut attempt = 1u32;
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(delay)).await;
            if app.sidecar.loaded().await.is_some() {
                return; // an Admin/Reload got there first
            }
            match app.load().await {
                Ok(_) => {
                    emit(
                        "info",
                        "initial load succeeded on retry",
                        json!({"attempt": attempt}),
                    );
                    return;
                }
                Err(e) => emit(
                    "warn",
                    "initial load retry failed",
                    json!({"attempt": attempt, "next_in_secs": (delay * 2).min(60), "error": e.message}),
                ),
            }
            delay = (delay * 2).min(60);
            attempt += 1;
        }
    });
}

fn tls(cfg: &Config) -> Result<Option<ServerTlsConfig>, String> {
    let Some((cert, key)) = &cfg.tls else {
        return Ok(None);
    };
    let read = |p: &std::path::Path| std::fs::read(p).map_err(|e| format!("{}: {e}", p.display()));
    Ok(Some(
        ServerTlsConfig::new().identity(Identity::from_pem(read(cert)?, read(key)?)),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn cfg(pairs: &[(&str, &str)]) -> Config {
        let mut m: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        m.entry("RAMEN_BUCKET".into()).or_insert(
            std::env::temp_dir()
                .join("ramen-nope")
                .display()
                .to_string(),
        );
        Config::from_map(&m).unwrap()
    }

    #[tokio::test]
    async fn serves_health_then_shuts_down() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        let server = tokio::spawn(run(cfg(&[]), listener, async move {
            let _ = rx.await;
        }));
        let ch = tonic::transport::Endpoint::from_shared(format!("http://{addr}"))
            .unwrap()
            .connect()
            .await
            .unwrap();
        let mut hc = tonic_health::pb::health_client::HealthClient::new(ch);
        let st = hc
            .check(tonic_health::pb::HealthCheckRequest {
                service: String::new(),
            })
            .await
            .unwrap()
            .into_inner()
            .status;
        assert_eq!(
            st,
            tonic_health::pb::health_check_response::ServingStatus::NotServing as i32
        );
        tx.send(()).unwrap();
        let app = server.await.unwrap();
        assert!(!app.sidecar.alive().await && app.sidecar.loaded().await.is_none());
    }

    #[tokio::test]
    async fn bucket_uri_triggers_initial_load_attempt() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let app = run(
            cfg(&[
                ("RAMEN_BUCKET_URI", "gs://b/g"),
                ("RAMEN_PYTHON", "/nonexistent/python"),
            ]),
            listener,
            async {},
        )
        .await;
        assert!(app.sidecar.loaded().await.is_none()); // spawn failed → warned, still serving
    }

    #[tokio::test]
    async fn missing_tls_files_fail_fast() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let c = cfg(&[
            ("RAMEN_TLS_CERT", "/nonexistent/c.pem"),
            ("RAMEN_TLS_KEY", "/nonexistent/k.pem"),
        ]);
        assert!(tls(&c).unwrap_err().contains("c.pem"));
        let app = run(c, listener, async {}).await;
        assert!(!app.sidecar.alive().await);
    }
}
