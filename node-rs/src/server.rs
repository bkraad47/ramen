//! Startup: initial `runtime.load` (when mcp/ exists or `RAMEN_BUCKET_URI` is set), serve gRPC AND Streamable HTTP
//! (§16.1) on one listener (h2c + HTTP/1.1, or TLS with `RAMEN_TLS_CERT`/`RAMEN_TLS_KEY`, re-read when renewed —
//! §39.2) until `shutdown`
//! resolves, then stop the sidecar.
use crate::config::Config;
use crate::grpc::{self, Shared};
use crate::log::emit;
use serde_json::json;
use tokio::net::TcpListener;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::transport::Server;

/// How long the node keeps serving after SIGTERM before it stops (`RAMEN_STOP_DRAIN_SECS`; default 20 in a pod,
/// 0 elsewhere). A pod that is told to stop is still in the load balancer's target list for a while — longest on
/// GKE's container-native NEGs — and callers routed to it must be served, not refused. Up to 0.8.0 this happened
/// by accident: the node is PID 1 in its container and installed no SIGTERM handler, so the kernel ignored the
/// signal and the pod served until the kubelet's kill at the end of the grace period (30 s).
pub fn stop_drain(configured: Option<&str>, in_kubernetes: bool) -> std::time::Duration {
    let secs = configured
        .and_then(|v| v.trim().parse::<u64>().ok())
        .unwrap_or(if in_kubernetes { 20 } else { 0 });
    std::time::Duration::from_secs(secs.min(300))
}

/// How long open streams get to flush their closing frame after the stop signal.
const STOP_GRACE: std::time::Duration = std::time::Duration::from_millis(300);

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
    if cfg.activator {
        emit(
            "info",
            "activator mode: no runtime; calls wake the zone and are forwarded",
            json!({"activator": true, "upstream": cfg.activator_upstream, "wake_timeout_secs": cfg.wake_timeout_secs}),
        );
    }
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
    // §24.1: an activator never starts a runtime — no reaper, no load (and `Admin/Reload` is refused)
    if app.activator.is_none() {
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
    }
    // §16.1: the HTTP handler joins the gRPC services on the same router; HTTP/1.1 must be accepted because
    // Streamable HTTP clients speak it (gRPC stays h2/h2c).
    let mut routes = grpc::routes(&app);
    let merged = std::mem::take(routes.axum_router_mut()).merge(crate::http::router(app.clone()));
    *routes.axum_router_mut() = merged;
    let server = Server::builder().accept_http1(true).add_routes(routes);
    // §23.6: a `subscriptions/listen` stream never ends by itself, so the signal first tells every stream to send
    // its completion result. Then the node stops for good after `STOP_GRACE`: tonic's graceful stop keeps the
    // listening socket open but unaccepted until the last connection ends, and a load balancer that still routes
    // to the pod then hangs new callers instead of being refused and retrying elsewhere (seen on kind, EKS and
    // GKE during rollouts in the 1.0.0 runs).
    let (stopping_tx, mut stopping) = tokio::sync::watch::channel(false);
    let shutdown = {
        let app = app.clone();
        async move {
            shutdown.await;
            app.subs.drain();
            let _ = stopping_tx.send(true);
        }
    };
    let serve = async {
        match tls(&cfg) {
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
        }
    };
    let served = tokio::select! {
        r = serve => r,
        _ = async {
            let _ = stopping.wait_for(|s| *s).await;
            tokio::time::sleep(STOP_GRACE).await;
        } => Ok(()),
    };
    if let Err(e) = served {
        emit("error", "serve failed", json!({"error": e.to_string()}));
    }
    app.sidecar.kill("shutdown").await;
    // §24.3: what the last calls produced still goes out — after the listener stopped, and for a second at most
    if app.otel.enabled() {
        let _ = tokio::time::timeout(std::time::Duration::from_secs(1), app.otel.flush()).await;
    }
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

/// §39.2: how often the serving pair is re-read (a renewed zone Secret reaches the pod's volume within a minute).
const RELOAD_EVERY: std::time::Duration = std::time::Duration::from_secs(60);

fn tls(cfg: &Config) -> Result<Option<tokio_rustls::TlsAcceptor>, String> {
    let Some((cert, key)) = &cfg.tls else {
        return Ok(None);
    };
    let pair = Reloading::load(cert.clone(), key.clone())?;
    spawn_reload(&pair, RELOAD_EVERY);
    Ok(Some(tokio_rustls::TlsAcceptor::from(server_config(pair))))
}

fn server_config(
    pair: std::sync::Arc<Reloading>,
) -> std::sync::Arc<tokio_rustls::rustls::ServerConfig> {
    let mut config = tokio_rustls::rustls::ServerConfig::builder()
        .with_no_client_auth()
        .with_cert_resolver(pair);
    config.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    std::sync::Arc::new(config)
}

/// Re-read the pair every `every` while the acceptor that serves it lives; a change is logged, a broken pair warned.
fn spawn_reload(pair: &std::sync::Arc<Reloading>, every: std::time::Duration) {
    let weak = std::sync::Arc::downgrade(pair);
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(every).await;
            let Some(pair) = weak.upgrade() else { return };
            match pair.refresh() {
                Ok(true) => emit("info", "tls certificate reloaded", json!({})),
                Ok(false) => {}
                Err(e) => emit(
                    "warn",
                    "tls certificate reload failed; serving the previous one",
                    json!({"error": e}),
                ),
            }
        }
    });
}

/// The node's serving pair (`RAMEN_TLS_CERT` / `RAMEN_TLS_KEY`), re-read by content (§39.2).
pub struct Reloading {
    cert: std::path::PathBuf,
    key: std::path::PathBuf,
    state: std::sync::RwLock<(
        Vec<u8>,
        Vec<u8>,
        std::sync::Arc<tokio_rustls::rustls::sign::CertifiedKey>,
    )>,
}

impl std::fmt::Debug for Reloading {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Reloading")
            .field("cert", &self.cert)
            .finish()
    }
}

fn certified(
    cert: &std::path::Path,
    key: &std::path::Path,
    cert_pem: &[u8],
    key_pem: &[u8],
) -> Result<tokio_rustls::rustls::sign::CertifiedKey, String> {
    use rustls_pki_types::pem::PemObject;
    use rustls_pki_types::{CertificateDer, PrivateKeyDer};
    let certs: Vec<CertificateDer<'static>> = CertificateDer::pem_slice_iter(cert_pem)
        .collect::<Result<_, _>>()
        .map_err(|e| format!("{}: {e}", cert.display()))?;
    if certs.is_empty() {
        return Err(format!("{}: no certificate", cert.display()));
    }
    let key =
        PrivateKeyDer::from_pem_slice(key_pem).map_err(|e| format!("{}: {e}", key.display()))?;
    tokio_rustls::rustls::sign::CertifiedKey::from_der(
        certs,
        key,
        &tokio_rustls::rustls::crypto::ring::default_provider(),
    )
    .map_err(|e| e.to_string())
}

impl Reloading {
    pub fn load(
        cert: std::path::PathBuf,
        key: std::path::PathBuf,
    ) -> Result<std::sync::Arc<Self>, String> {
        let read =
            |p: &std::path::Path| std::fs::read(p).map_err(|e| format!("{}: {e}", p.display()));
        let (c, k) = (read(&cert)?, read(&key)?);
        let ck = certified(&cert, &key, &c, &k)?;
        Ok(std::sync::Arc::new(Reloading {
            cert,
            key,
            state: std::sync::RwLock::new((c, k, std::sync::Arc::new(ck))),
        }))
    }

    pub fn current(&self) -> std::sync::Arc<tokio_rustls::rustls::sign::CertifiedKey> {
        self.state
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .2
            .clone()
    }

    /// Re-read both files: `Ok(true)` when the pair changed and is now served, `Ok(false)` when unchanged, an error
    /// (the old pair kept) when the new one does not load.
    pub fn refresh(&self) -> Result<bool, String> {
        let read =
            |p: &std::path::Path| std::fs::read(p).map_err(|e| format!("{}: {e}", p.display()));
        let (c, k) = (read(&self.cert)?, read(&self.key)?);
        {
            let st = self.state.read().unwrap_or_else(|e| e.into_inner());
            if st.0 == c && st.1 == k {
                return Ok(false);
            }
        }
        let ck = certified(&self.cert, &self.key, &c, &k)?;
        *self.state.write().unwrap_or_else(|e| e.into_inner()) = (c, k, std::sync::Arc::new(ck));
        Ok(true)
    }
}

impl tokio_rustls::rustls::server::ResolvesServerCert for Reloading {
    fn resolve(
        &self,
        _: tokio_rustls::rustls::server::ClientHello<'_>,
    ) -> Option<std::sync::Arc<tokio_rustls::rustls::sign::CertifiedKey>> {
        Some(self.current())
    }
}

/// Why an accepted connection did not become a TLS stream.
#[derive(Debug, PartialEq)]
enum Refused {
    /// The peer closed (or reset) before sending a byte: a TCP-level probe, not a TLS failure.
    NoHello(String),
    /// b35: a handshake that started and then the peer went away (EOF or reset mid-ClientHello): a health checker
    /// that gives up after the TCP part (GFE 35.191.x, 169.254.4.6), not a TLS failure.
    Abandoned(String),
    /// A handshake that started and failed (a wrong protocol, an alert from the peer — it does not trust our leaf —,
    /// one of ours — no common version or cipher —, …).
    Handshake(String),
}

/// The level and message each refusal is logged with (§39.5a): the peer saying nothing or giving up is `debug`;
/// anything that names a TLS problem is `warn`.
fn refusal_log(r: &Refused) -> (&'static str, &'static str) {
    match r {
        Refused::NoHello(_) => ("debug", "connection closed before a tls ClientHello"),
        Refused::Abandoned(_) => ("debug", "tls handshake abandoned by peer"),
        Refused::Handshake(_) => ("warn", "tls handshake failed"),
    }
}

/// One handshake: the first byte is awaited (without consuming it) so a connection that ends before any ClientHello
/// is told apart from a handshake that failed.
async fn accept(
    acceptor: &tokio_rustls::TlsAcceptor,
    stream: tokio::net::TcpStream,
) -> Result<tokio_rustls::server::TlsStream<tokio::net::TcpStream>, Refused> {
    match stream.peek(&mut [0u8; 1]).await {
        Ok(0) => return Err(Refused::NoHello("eof".into())),
        Err(e) => return Err(Refused::NoHello(e.to_string())),
        Ok(_) => {}
    }
    acceptor.accept(stream).await.map_err(|e| {
        use std::io::ErrorKind::*;
        // rustls's own errors (alerts sent or received, bad records) come as `InvalidData`; the transport's as these
        match e.kind() {
            UnexpectedEof | ConnectionReset | ConnectionAborted | BrokenPipe => {
                Refused::Abandoned(e.to_string())
            }
            _ => Refused::Handshake(e.to_string()),
        }
    })
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
                    accept(&acceptor, stream),
                );
                match handshake.await {
                    Ok(Ok(tls)) => {
                        let _ = tx.send(Ok(tls)).await;
                    }
                    // a load balancer's or kubelet's TCP check: connect, then close without a word or mid-ClientHello
                    // (b33/b35: 1–4 per new pod from GFE health checkers and link-local probes)
                    Ok(Err(r)) => {
                        let (level, msg) = refusal_log(&r);
                        let (Refused::NoHello(e) | Refused::Abandoned(e) | Refused::Handshake(e)) =
                            r;
                        emit(level, msg, json!({"peer": peer, "error": e}));
                    }
                    Err(_) => emit("warn", "tls handshake timed out", json!({"peer": peer})),
                }
            });
        }
    });
    tokio_stream::wrappers::ReceiverStream::new(rx)
}

#[cfg(test)]
mod tests {
    #[test]
    fn a_pod_drains_by_default_and_a_laptop_does_not() {
        use super::stop_drain;
        use std::time::Duration;
        assert_eq!(stop_drain(None, true), Duration::from_secs(20));
        assert_eq!(stop_drain(None, false), Duration::ZERO);
        assert_eq!(stop_drain(Some("5"), false), Duration::from_secs(5));
        assert_eq!(stop_drain(Some("0"), true), Duration::ZERO);
        assert_eq!(stop_drain(Some("nonsense"), true), Duration::from_secs(20));
        assert_eq!(stop_drain(Some("9999"), true), Duration::from_secs(300));
    }

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

    /// A GET over h2c/HTTP/1.1 or, with `ca`, over TLS verified against it as `localhost`: the status code.
    async fn get(addr: std::net::SocketAddr, path: &str, ca: Option<&str>) -> u16 {
        use http_body_util::Empty;
        use hyper::body::Bytes;
        use hyper_util::rt::TokioIo;
        let tcp = tokio::net::TcpStream::connect(addr).await.unwrap();
        let req = http::Request::get(path)
            .header("host", "localhost")
            .body(Empty::<Bytes>::new())
            .unwrap();
        async fn send<I>(io: I, req: http::Request<Empty<Bytes>>) -> u16
        where
            I: hyper::rt::Read + hyper::rt::Write + Unpin + Send + 'static,
        {
            let (mut tx, conn) = hyper::client::conn::http1::handshake(io).await.unwrap();
            tokio::spawn(conn);
            tx.send_request(req).await.unwrap().status().as_u16()
        }
        match ca {
            None => send(TokioIo::new(tcp), req).await,
            Some(pem) => {
                use rustls_pki_types::CertificateDer;
                use rustls_pki_types::pem::PemObject;
                let mut roots = tokio_rustls::rustls::RootCertStore::empty();
                roots
                    .add(CertificateDer::from_pem_slice(pem.as_bytes()).unwrap())
                    .unwrap();
                let config = tokio_rustls::rustls::ClientConfig::builder()
                    .with_root_certificates(roots)
                    .with_no_client_auth();
                let tls = tokio_rustls::TlsConnector::from(std::sync::Arc::new(config))
                    .connect("localhost".try_into().unwrap(), tcp)
                    .await
                    .unwrap();
                send(TokioIo::new(tls), req).await
            }
        }
    }

    /// §39.3 (D86): kubelet probes `GET /readyz` (the `""` health: SERVING once loaded) and `GET /healthz` (alive)
    /// over the node's TLS — its gRPC probe cannot speak TLS — and over h2c on an install without TLS.
    #[tokio::test]
    async fn readyz_and_healthz_answer_over_tls_and_h2c() {
        let ca = crate::testca::TestCa::new();
        let dir = crate::testca::temp_dir("probe");
        let (bundle, cert, key) = ca.write(&dir, &["localhost"]);
        let pair = [
            ("RAMEN_TLS_CERT", cert.to_str().unwrap()),
            ("RAMEN_TLS_KEY", key.to_str().unwrap()),
            ("RAMEN_TLS_CA", bundle.to_str().unwrap()),
        ];
        for (extra, ready) in [(vec![], 503), (vec![("RAMEN_ACTIVATOR", "1")], 200)] {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let addr = listener.local_addr().unwrap();
            let (tx, rx) = tokio::sync::oneshot::channel::<()>();
            let mut pairs = pair.to_vec();
            pairs.extend(extra);
            pairs.push(("RAMEN_ACTIVATOR_UPSTREAM", "127.0.0.1:1"));
            let server = tokio::spawn(run(cfg(&pairs), listener, async move {
                let _ = rx.await;
            }));
            let pem = ca.pem();
            assert_eq!(get(addr, "/healthz", Some(&pem)).await, 200);
            assert_eq!(get(addr, "/readyz", Some(&pem)).await, ready);
            tx.send(()).unwrap();
            server.await.unwrap();
        }
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        let server = tokio::spawn(run(cfg(&[]), listener, async move {
            let _ = rx.await;
        }));
        assert_eq!(get(addr, "/healthz", None).await, 200);
        assert_eq!(get(addr, "/readyz", None).await, 503);
        tx.send(()).unwrap();
        server.await.unwrap();
    }

    /// §39.2: a renewed pair is served without a restart. Kubernetes swaps a Secret volume by re-pointing the
    /// `..data` symlink, so the files are compared by content; a broken new pair keeps the old one.
    #[tokio::test]
    async fn a_renewed_pair_is_served_without_a_restart() {
        use std::os::unix::fs::symlink;
        let ca = crate::testca::TestCa::new();
        let dir = crate::testca::temp_dir("reload");
        let version = |n: &str, names: &[&str]| {
            let d = dir.join(n);
            ca.write(&d, names);
            d
        };
        version("..v1", &["localhost", "first.example"]);
        symlink("..v1", dir.join("..data")).unwrap();
        for f in ["tls.crt", "tls.key"] {
            symlink(format!("..data/{f}"), dir.join(f)).unwrap();
        }
        let r = Reloading::load(dir.join("tls.crt"), dir.join("tls.key")).unwrap();
        let served = |r: &std::sync::Arc<Reloading>| {
            let leaf = &r.current().cert[0];
            let text = String::from_utf8_lossy(leaf.as_ref()).to_string();
            (
                text.contains("first.example"),
                text.contains("second.example"),
            )
        };
        assert_eq!(served(&r), (true, false));
        assert!(!r.refresh().unwrap(), "nothing changed");
        version("..v2", &["localhost", "second.example"]);
        symlink("..v2", dir.join("..tmp")).unwrap();
        std::fs::rename(dir.join("..tmp"), dir.join("..data")).unwrap();
        assert!(r.refresh().unwrap());
        assert_eq!(served(&r), (false, true));
        // a handshake now gets the new leaf
        let acceptor = tokio_rustls::TlsAcceptor::from(server_config(r.clone()));
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            let (s, _) = listener.accept().await.unwrap();
            let _ = acceptor.accept(s).await;
        });
        let mut roots = tokio_rustls::rustls::RootCertStore::empty();
        roots
            .add(rustls_pki_types::pem::PemObject::from_pem_slice(ca.pem().as_bytes()).unwrap())
            .unwrap();
        let client = tokio_rustls::rustls::ClientConfig::builder()
            .with_root_certificates(roots)
            .with_no_client_auth();
        let tls = tokio_rustls::TlsConnector::from(std::sync::Arc::new(client))
            .connect(
                "second.example".try_into().unwrap(),
                tokio::net::TcpStream::connect(addr).await.unwrap(),
            )
            .await;
        assert!(tls.is_ok(), "{:?}", tls.err());
        // a half-written renewal (key without its certificate) is refused and the served pair stays
        std::fs::write(dir.join("..v2").join("tls.key"), "not a key").unwrap();
        assert!(r.refresh().is_err());
        assert_eq!(served(&r), (false, true));
    }

    /// b33: a TCP probe that closes before a ClientHello is not a handshake failure (logged at debug); a peer that
    /// starts a handshake and fails, or speaks another protocol, still is (warn).
    #[tokio::test]
    async fn a_probe_that_closes_before_a_client_hello_is_not_a_handshake_failure() {
        use tokio::io::AsyncWriteExt;
        let ca = crate::testca::TestCa::new();
        let dir = crate::testca::temp_dir("probe-eof");
        let (_, cert, key) = ca.write(&dir, &["localhost"]);
        let acceptor =
            tokio_rustls::TlsAcceptor::from(server_config(Reloading::load(cert, key).unwrap()));
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        // closes at once
        drop(tokio::net::TcpStream::connect(addr).await.unwrap());
        let (s, _) = listener.accept().await.unwrap();
        assert!(matches!(
            accept(&acceptor, s).await,
            Err(Refused::NoHello(_))
        ));
        // plain HTTP to the TLS port
        let mut c = tokio::net::TcpStream::connect(addr).await.unwrap();
        c.write_all(b"GET / HTTP/1.1\r\nhost: x\r\n\r\n")
            .await
            .unwrap();
        let (s, _) = listener.accept().await.unwrap();
        assert!(matches!(
            accept(&acceptor, s).await,
            Err(Refused::Handshake(_))
        ));
        // a ClientHello, then the client gives up (it does not trust the server)
        let other = crate::testca::TestCa::new();
        let mut roots = tokio_rustls::rustls::RootCertStore::empty();
        roots
            .add(rustls_pki_types::pem::PemObject::from_pem_slice(other.pem().as_bytes()).unwrap())
            .unwrap();
        let client = tokio_rustls::rustls::ClientConfig::builder()
            .with_root_certificates(roots)
            .with_no_client_auth();
        let dial = tokio::spawn(async move {
            let tcp = tokio::net::TcpStream::connect(addr).await.unwrap();
            tokio_rustls::TlsConnector::from(std::sync::Arc::new(client))
                .connect("localhost".try_into().unwrap(), tcp)
                .await
                .is_err()
        });
        let (s, _) = listener.accept().await.unwrap();
        assert!(matches!(
            accept(&acceptor, s).await,
            Err(Refused::Handshake(_))
        ));
        assert!(dial.await.unwrap());
        // a good client
        let pem = ca.pem();
        let good = tokio::spawn(async move {
            let mut roots = tokio_rustls::rustls::RootCertStore::empty();
            roots
                .add(rustls_pki_types::pem::PemObject::from_pem_slice(pem.as_bytes()).unwrap())
                .unwrap();
            let client = tokio_rustls::rustls::ClientConfig::builder()
                .with_root_certificates(roots)
                .with_no_client_auth();
            let tcp = tokio::net::TcpStream::connect(addr).await.unwrap();
            tokio_rustls::TlsConnector::from(std::sync::Arc::new(client))
                .connect("localhost".try_into().unwrap(), tcp)
                .await
                .is_ok()
        });
        let (s, _) = listener.accept().await.unwrap();
        assert!(accept(&acceptor, s).await.is_ok());
        assert!(good.await.unwrap());
    }

    /// b35: GFE health checkers (35.191.x) and 169.254.4.6 pass the 1-byte peek, start a ClientHello and close: the
    /// peer gave up (EOF or reset mid-handshake), which is not a failure of ours (debug). A peer that answers with an
    /// alert (it does not trust our leaf) or speaks another protocol still is (warn: `a_probe_that_closes…`).
    #[tokio::test]
    async fn a_handshake_the_peer_abandons_is_not_a_handshake_failure() {
        use tokio::io::AsyncWriteExt;
        let ca = crate::testca::TestCa::new();
        let dir = crate::testca::temp_dir("probe-abandon");
        let (_, cert, key) = ca.write(&dir, &["localhost"]);
        let acceptor =
            tokio_rustls::TlsAcceptor::from(server_config(Reloading::load(cert, key).unwrap()));
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        // a handshake record header and part of a ClientHello, then a clean close
        let partial = [
            0x16, 0x03, 0x01, 0x00, 0xc8, 0x01, 0x00, 0x00, 0xc4, 0x03, 0x03,
        ];
        let mut c = tokio::net::TcpStream::connect(addr).await.unwrap();
        c.write_all(&partial).await.unwrap();
        c.shutdown().await.unwrap();
        let (s, _) = listener.accept().await.unwrap();
        let got = accept(&acceptor, s).await;
        assert!(matches!(got, Err(Refused::Abandoned(_))), "{got:?}");
        drop(c);
        // the same, then a reset
        let mut c = tokio::net::TcpStream::connect(addr).await.unwrap();
        let (s, _) = listener.accept().await.unwrap();
        c.write_all(&partial).await.unwrap();
        c.set_zero_linger().unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(50)).await; // the bytes arrive before the RST
        drop(c);
        let got = accept(&acceptor, s).await;
        assert!(matches!(got, Err(Refused::Abandoned(_))), "{got:?}");
        assert_eq!(refusal_log(&got.unwrap_err()).0, "debug");
        assert_eq!(
            refusal_log(&Refused::Abandoned("x".into())).1,
            "tls handshake abandoned by peer"
        );
        assert_eq!(
            refusal_log(&Refused::Handshake("x".into())),
            ("warn", "tls handshake failed")
        );
        assert_eq!(refusal_log(&Refused::NoHello("x".into())).0, "debug");
    }

    /// §24.1: an activator is SERVING from the start, with a bucket that would make a worker load — and never
    /// starts a runtime, at startup or on shutdown.
    #[tokio::test]
    async fn an_activator_serves_health_at_once_and_starts_no_runtime() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        let c = cfg(&[
            ("RAMEN_ACTIVATOR", "1"),
            ("RAMEN_ACTIVATOR_UPSTREAM", "127.0.0.1:1"),
            ("RAMEN_BUCKET_URI", "gs://b/g"),
            ("RAMEN_PYTHON", "/nonexistent/python"),
        ]);
        let server = tokio::spawn(run(c, listener, async move {
            let _ = rx.await;
        }));
        let ch = tonic::transport::Endpoint::from_shared(format!("http://{addr}"))
            .unwrap()
            .connect()
            .await
            .unwrap();
        let st = tonic_health::pb::health_client::HealthClient::new(ch)
            .check(tonic_health::pb::HealthCheckRequest {
                service: String::new(),
            })
            .await
            .unwrap()
            .into_inner()
            .status;
        assert_eq!(
            st,
            tonic_health::pb::health_check_response::ServingStatus::Serving as i32
        );
        tx.send(()).unwrap();
        let app = server.await.unwrap();
        assert!(app.activator.is_some());
        assert!(!app.sidecar.alive().await && app.sidecar.loaded().await.is_none());
    }
}
