//! Startup: initial `runtime.load` (when mcp/ exists or `RAMEN_BUCKET_URI` is set), serve gRPC AND Streamable HTTP
//! (§16.1) on one listener (h2c + HTTP/1.1, or TLS with `RAMEN_TLS_CERT`/`RAMEN_TLS_KEY`) until `shutdown`
//! resolves, then stop the sidecar.
use crate::config::Config;
use crate::grpc::{self, Shared};
use crate::log::emit;
use serde_json::json;
use tokio::net::TcpListener;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::transport::Server;

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
    {
        // v0.5.5: `FILE` (log.rs) is process-global; cargo test runs tests on parallel threads, so this
        // ordinary startup side effect can redirect it mid-way through an unrelated test that briefly needs
        // it pinned (log::tests::mirrors_to_file). Block on the same lock that test holds, so `set_file`
        // here only runs before or after — never during — that test's short critical section. No-op cost
        // outside `cfg(test)`.
        #[cfg(test)]
        let _log_guard = crate::log::TEST_LOCK
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        crate::log::set_file(cfg.log_file.clone());
    }
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
    // §16.1: the HTTP handler joins the gRPC services on the same router; HTTP/1.1 must be accepted because
    // Streamable HTTP clients speak it (gRPC stays h2/h2c).
    let mut routes = grpc::routes(&app);
    let merged = std::mem::take(routes.axum_router_mut()).merge(crate::http::router(app.clone()));
    *routes.axum_router_mut() = merged;
    let server = Server::builder().accept_http1(true).add_routes(routes);
    let served = match tls(&cfg) {
        // TLS is terminated here, not by tonic: its acceptor offers only `h2` on ALPN, and an HTTP/1.1 client
        // (every Streamable HTTP client) is then refused at the handshake. Found by CI's TLS case after 0.5.0.
        Ok(Some(acceptor)) => {
            server
                .serve_with_incoming_shutdown(tls_incoming(listener, acceptor), shutdown)
                .await
        }
        Ok(None) => {
            server
                .serve_with_incoming_shutdown(TcpListenerStream::new(listener), shutdown)
                .await
        }
        Err(e) => {
            emit("error", "tls config failed", json!({"error": e}));
            Ok(())
        }
    };
    if let Err(e) = served {
        emit("error", "serve failed", json!({"error": e.to_string()}));
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

fn tls(cfg: &Config) -> Result<Option<tokio_rustls::TlsAcceptor>, String> {
    use rustls_pki_types::pem::PemObject;
    use rustls_pki_types::{CertificateDer, PrivateKeyDer};
    let Some((cert, key)) = &cfg.tls else {
        return Ok(None);
    };
    let read = |p: &std::path::Path| std::fs::read(p).map_err(|e| format!("{}: {e}", p.display()));
    let certs: Vec<CertificateDer<'static>> = CertificateDer::pem_slice_iter(&read(cert)?)
        .collect::<Result<_, _>>()
        .map_err(|e| format!("{}: {e}", cert.display()))?;
    let key = PrivateKeyDer::from_pem_slice(&read(key)?)
        .map_err(|e| format!("{}: {e}", key.display()))?;
    let mut config = tokio_rustls::rustls::ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(certs, key)
        .map_err(|e| e.to_string())?;
    config.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    Ok(Some(tokio_rustls::TlsAcceptor::from(std::sync::Arc::new(
        config,
    ))))
}

/// Accept TCP connections and hand each one to the TLS acceptor on its own task, so one slow or hostile handshake
/// never holds the accept loop. Only completed handshakes reach the server; a failed one is a log line.
fn tls_incoming(
    listener: tokio::net::TcpListener,
    acceptor: tokio_rustls::TlsAcceptor,
) -> tokio_stream::wrappers::ReceiverStream<
    Result<tokio_rustls::server::TlsStream<tokio::net::TcpStream>, std::io::Error>,
> {
    let (tx, rx) = tokio::sync::mpsc::channel(64);
    tokio::spawn(async move {
        loop {
            let (stream, peer) = match listener.accept().await {
                Ok(x) => x,
                Err(e) => {
                    emit("warn", "accept failed", json!({"error": e.to_string()}));
                    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                    continue;
                }
            };
            let acceptor = acceptor.clone();
            let tx = tx.clone();
            tokio::spawn(async move {
                let handshake = tokio::time::timeout(
                    std::time::Duration::from_secs(10),
                    acceptor.accept(stream),
                );
                match handshake.await {
                    Ok(Ok(tls)) => {
                        let _ = tx.send(Ok(tls)).await;
                    }
                    Ok(Err(e)) => emit(
                        "warn",
                        "tls handshake failed",
                        json!({"peer": peer, "error": e.to_string()}),
                    ),
                    Err(_) => emit("warn", "tls handshake timed out", json!({"peer": peer})),
                }
            });
        }
    });
    tokio_stream::wrappers::ReceiverStream::new(rx)
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
        assert!(
            tls(&c)
                .err()
                .expect("missing files must fail")
                .contains("c.pem")
        );
        let app = run(c, listener, async {}).await;
        assert!(!app.sidecar.alive().await);
    }
}
