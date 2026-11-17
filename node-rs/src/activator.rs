//! Activator mode (CONTRACTS §24.1, `RAMEN_ACTIVATOR=1`): the same binary standing in for a sleeping zone's
//! workers behind the zone's `worker` Service.
//!
//! It runs the zone's own guards — `grpc::guard` and the first half of `grpc::dispatch_call` (address, Origin,
//! credential, era checks, throttle), so a call the workers would refuse never wakes them — and no runtime, ever.
//! A call that gets through asks the console to wake the zone (one request per wake; concurrent calls share it; a
//! failed request is retried by the next call), waits until `RAMEN_ACTIVATOR_UPSTREAM` answers gRPC health
//! SERVING, then forwards the request unchanged and relays the answer: HTTP with its headers and body, the
//! response streamed through as it arrives; gRPC `Mcp/Call` bytes with the caller's metadata. While the workers
//! are up it simply forwards (the console removes the activator a little later). Not SERVING within
//! `RAMEN_WAKE_TIMEOUT_SECS` → HTTP 503 + `Retry-After: 10` / gRPC UNAVAILABLE, logged with reason `waking`.
//!
//! The telemetry consent gate (§24.3) is not applied here: the worker applies it to the forwarded call.
use crate::config::Config;
use crate::grpc::{ACTIVATOR_HEADER, CLIENT_IP_HEADER, CallLog, MAX_MESSAGE_BYTES};
use crate::log::emit;
use crate::pb::JsonRpc;
use crate::pb::mcp_client::McpClient;
use axum::body::{Body, Bytes};
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, Uri, Version, header};
use axum::response::{IntoResponse, Response};
use hyper_util::client::legacy::Client;
use hyper_util::client::legacy::connect::{Connected, Connection};
use hyper_util::rt::TokioIo;
use serde_json::{Value, json};
use std::sync::atomic::{AtomicBool, AtomicU8, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::watch;
use tonic::metadata::{KeyAndValueRef, MetadataMap};
use tonic::transport::{Channel, Endpoint};
use tonic::{Code, Request, Status};
use tonic_health::pb::health_check_response::ServingStatus;
use tonic_health::pb::{HealthCheckRequest, health_client::HealthClient};

pub const RETRY_AFTER_SECS: &str = "10";
/// A SERVING answer is trusted this long before the next call checks again: the workers leave shortly after an
/// activator appears (it is applied first, then they are scaled to 0).
const FRESH: Duration = Duration::from_secs(1);
const HEALTH_TIMEOUT: Duration = Duration::from_secs(2);

// one wake at a time: nothing asked / the request is on its way / the console took it
const IDLE: u8 = 0;
const POSTING: u8 = 1;
const POSTED: u8 = 2;
/// A taken wake request is believed this long; after it an accepted call asks again (the console's wake is
/// idempotent). An activator pod can outlive a wake — the console scales it to 0 and, when the zone is put back to
/// sleep at once, to 1 again before the pod was removed — and must not go on believing the last wake (kind, 1.0.0).
const REPOST: Duration = Duration::from_secs(5);

pub struct Activator {
    group: String,
    zone: String,
    wake_url: Option<String>,
    admin_key: Option<String>,
    /// §39.2: `RAMEN_TLS_CA`, trusted for the console's `https://` wake endpoint.
    ca: Option<std::path::PathBuf>,
    timeout: Duration,
    /// `grpc::activator_proof`: sent with every forwarded call so the workers take the client address from us.
    proof: String,
    /// `https://host:port` of the zone's real workers (`Service/worker-direct`) — TLS under the install CA, verified
    /// as `RAMEN_TLS_SERVER_NAME` (§39.3) — or `http://host:port` (h2c) on an install without TLS.
    upstream: String,
    /// The gRPC channel and the bundle it verifies with (rebuilt when `trust`'s bundle changes).
    channel: Mutex<(Vec<u8>, Channel)>,
    endpoint: Endpoint,
    trust: Option<Arc<Trust>>,
    http: Client<UpstreamConnector, Body>,
    wake: AtomicU8,
    polling: AtomicBool,
    /// Counts SERVING observations by the poller; waiting calls watch it change.
    up: watch::Sender<u64>,
    up_at: Mutex<Option<Instant>>,
    /// When the console last took a wake request (`REPOST`).
    posted_at: Mutex<Option<Instant>>,
    /// Wake requests the console accepted.
    pub woken: AtomicU64,
    /// The health poll period while waking, in ms (500; tests shorten it).
    pub poll_ms: AtomicU64,
}

/// What a call found on its way to the workers.
pub enum Ready {
    /// The workers answered at once.
    Up,
    /// They answered after this call waited for the wake.
    Woke,
}

fn hop_by_hop(name: &header::HeaderName) -> bool {
    matches!(
        name.as_str(),
        "connection"
            | "keep-alive"
            | "proxy-connection"
            | "transfer-encoding"
            | "upgrade"
            | "te"
            | "trailer"
            | "host"
            | "content-length"
    )
}

impl Activator {
    pub fn new(cfg: &Config, proof: String) -> Result<Arc<Self>, String> {
        let (tls, hostport) = match cfg.activator_upstream.split_once("://") {
            Some(("https", rest)) => (true, rest),
            Some(("http", rest)) => (false, rest),
            Some(_) => {
                return Err(
                    "RAMEN_ACTIVATOR_UPSTREAM must be host:port, http://host:port or https://host:port"
                        .into(),
                );
            }
            // §39.3: a worker that serves TLS, or knows the install CA, dials its peers over TLS
            None => (
                cfg.tls.is_some() || cfg.tls_ca.is_some(),
                cfg.activator_upstream.as_str(),
            ),
        };
        let upstream = format!("{}://{hostport}", if tls { "https" } else { "http" });
        let endpoint = Endpoint::from_shared(upstream.clone())
            .map_err(|e| format!("RAMEN_ACTIVATOR_UPSTREAM: {e}"))?
            .connect_timeout(HEALTH_TIMEOUT);
        let trust = match tls {
            true => {
                let path = cfg.tls_ca.as_ref().ok_or(
                    "a TLS upstream needs RAMEN_TLS_CA (the install CA bundle) to verify the workers with",
                )?;
                Some(Arc::new(Trust::load(path, &cfg.tls_server_name)?))
            }
            false => None,
        };
        let connector = UpstreamConnector { tls: trust.clone() };
        let channel = match &trust {
            Some(t) => {
                let pem = t.current().0;
                let ch = tls_channel(&endpoint, &pem, &t.name)?;
                (pem, ch)
            }
            None => (Vec::new(), endpoint.connect_lazy()),
        };
        let http = Client::builder(hyper_util::rt::TokioExecutor::new())
            .http2_only(true)
            .build(connector);
        if cfg.console_internal_url.is_none() {
            emit(
                "warn",
                "RAMEN_CONSOLE_INTERNAL_URL unset: calls wait for the zone but cannot wake it",
                json!({"activator": true}),
            );
        }
        Ok(Arc::new(Activator {
            group: cfg.group.clone(),
            zone: cfg.zone.clone(),
            wake_url: cfg.console_internal_url.as_ref().map(|base| {
                format!(
                    "{base}/api/v1/internal/zones/{}/{}/wake",
                    cfg.group, cfg.zone
                )
            }),
            admin_key: cfg.admin_key.clone(),
            ca: cfg.tls_ca.clone(),
            timeout: Duration::from_secs(cfg.wake_timeout_secs),
            proof,
            upstream,
            channel: Mutex::new(channel),
            endpoint,
            trust,
            http,
            wake: AtomicU8::new(IDLE),
            polling: AtomicBool::new(false),
            up: watch::channel(0).0,
            up_at: Mutex::new(None),
            posted_at: Mutex::new(None),
            woken: AtomicU64::new(0),
            poll_ms: AtomicU64::new(500),
        }))
    }

    #[cfg(test)]
    fn new_for_test(cfg: &Config) -> Result<Arc<Self>, String> {
        Self::new(cfg, "proof".into())
    }

    /// `Admin/Metrics` of an activator: the zone is asleep as far as the console's idle check is concerned.
    pub fn metrics_json(&self, last_call_at: u64) -> Value {
        json!({"asleep": true, "last_call_at": last_call_at, "woken": self.woken.load(Relaxed)})
    }

    async fn serving(&self) -> bool {
        let mut client = HealthClient::new(self.channel());
        let check = client.check(HealthCheckRequest {
            service: String::new(),
        });
        matches!(
            tokio::time::timeout(HEALTH_TIMEOUT, check).await,
            Ok(Ok(r)) if r.get_ref().status == ServingStatus::Serving as i32
        )
    }

    /// The gRPC channel to the workers, rebuilt when the install CA bundle changed (§39.1: a rotation needs no
    /// restart). A bundle that does not load keeps the channel built with the last good one.
    fn channel(&self) -> Channel {
        let mut ch = self.channel.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(t) = &self.trust {
            let pem = t.current().0;
            if pem != ch.0
                && let Ok(fresh) = tls_channel(&self.endpoint, &pem, &t.name)
            {
                *ch = (pem, fresh);
            }
        }
        ch.1.clone()
    }

    fn mark_up(&self) {
        *self.up_at.lock().unwrap_or_else(|e| e.into_inner()) = Some(Instant::now());
    }

    fn mark_down(&self) {
        *self.up_at.lock().unwrap_or_else(|e| e.into_inner()) = None;
    }

    fn fresh(&self) -> bool {
        self.up_at
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .is_some_and(|at| at.elapsed() < FRESH)
    }

    /// Ask the console to wake the zone, unless a request is under way or was accepted for this wake. Never waits:
    /// the console may answer only when the workers are Ready, and the callers poll for that themselves.
    fn request_wake(self: &Arc<Self>) {
        let stale = self
            .posted_at
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .is_none_or(|at| at.elapsed() >= REPOST);
        let from = if stale { POSTED } else { IDLE };
        if self
            .wake
            .compare_exchange(IDLE, POSTING, Relaxed, Relaxed)
            .is_err()
            && (from == IDLE
                || self
                    .wake
                    .compare_exchange(POSTED, POSTING, Relaxed, Relaxed)
                    .is_err())
        {
            return;
        }
        let me = self.clone();
        tokio::spawn(async move {
            let Some(url) = &me.wake_url else {
                me.wake.store(IDLE, Relaxed);
                return;
            };
            let headers: Vec<(String, String)> = me
                .admin_key
                .iter()
                .map(|k| ("x-ramen-admin-key".to_string(), k.clone()))
                .collect();
            let body = json!({"trigger": "call"}).to_string().into_bytes();
            let fields = |extra: Value| {
                let mut f = json!({"activator": true, "group": me.group, "zone": me.zone, "trigger": "call"});
                f.as_object_mut()
                    .unwrap()
                    .extend(extra.as_object().cloned().unwrap_or_default());
                f
            };
            match crate::netc::post_json(url, &headers, body, me.timeout, me.ca.as_deref()).await {
                Ok((status, _)) if (200..300).contains(&status) => {
                    me.woken.fetch_add(1, Relaxed);
                    // IDLE again if the workers came up meanwhile (`mark_up`); otherwise this wake is asked for
                    *me.posted_at.lock().unwrap_or_else(|e| e.into_inner()) = Some(Instant::now());
                    let _ = me.wake.compare_exchange(POSTING, POSTED, Relaxed, Relaxed);
                    emit("info", "wake requested", fields(json!({"status": status})));
                }
                Ok((status, _)) => {
                    me.wake.store(IDLE, Relaxed);
                    emit(
                        "warn",
                        "wake request refused",
                        fields(json!({"status": status})),
                    );
                }
                Err(e) => {
                    me.wake.store(IDLE, Relaxed);
                    emit("warn", "wake request failed", fields(json!({"error": e})));
                }
            }
        });
    }

    /// One poller for however many calls wait; it ends at the first SERVING answer or after the wake timeout.
    fn start_poller(self: &Arc<Self>) {
        if self.polling.swap(true, Relaxed) {
            return;
        }
        let me = self.clone();
        tokio::spawn(async move {
            let deadline = Instant::now() + me.timeout;
            loop {
                if me.serving().await {
                    me.mark_up();
                    me.up.send_modify(|n| *n += 1);
                    break;
                }
                if Instant::now() >= deadline {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(me.poll_ms.load(Relaxed).max(1))).await;
            }
            me.polling.store(false, Relaxed);
        });
    }

    /// Returns once the workers answer SERVING; `Err` when they did not within the wake timeout.
    pub async fn ready(self: &Arc<Self>) -> Result<Ready, Status> {
        // An accepted call always asks for the wake (once: `request_wake` is a no-op while one is posted). A SERVING
        // upstream does not mean the zone is awake — right after a sleep the old workers still answer while they
        // drain, and a call they served used to leave the zone asleep with nobody behind it (kind run, 1.0.0).
        self.request_wake();
        if self.fresh() {
            return Ok(Ready::Up);
        }
        if self.serving().await {
            self.mark_up();
            return Ok(Ready::Up);
        }
        self.mark_down();
        let mut up = self.up.subscribe();
        let deadline = tokio::time::sleep(self.timeout);
        tokio::pin!(deadline);
        loop {
            // (re)started on every turn: a poller that gave up just before this call arrived is replaced
            self.start_poller();
            tokio::select! {
                _ = &mut deadline => {
                    // the console took the request and the workers did not come in time: the next call asks
                    // again (a wake is idempotent on the console's side)
                    let _ = self.wake.compare_exchange(POSTED, IDLE, Relaxed, Relaxed);
                    return Err(Status::unavailable("waking: the zone is starting, retry shortly"));
                }
                r = up.changed() => {
                    if r.is_ok() {
                        return Ok(Ready::Woke);
                    }
                }
                _ = tokio::time::sleep(Duration::from_millis(self.poll_ms.load(Relaxed).max(1))) => {}
            }
        }
    }

    /// The gRPC half: `Mcp/Call` bytes and the caller's metadata to the workers, their answer (or status) back.
    pub async fn grpc(
        self: &Arc<Self>,
        cfg: &Config,
        log: &CallLog,
        md: &MetadataMap,
        body: Vec<u8>,
    ) -> Result<tonic::Response<JsonRpc>, Status> {
        let woke = match self.ready().await {
            Ok(r) => matches!(r, Ready::Woke),
            Err(st) => return Err(log.deny(cfg, st, "waking")),
        };
        let mut req = Request::new(JsonRpc { body });
        // the caller's metadata as it came; what describes the hop (encoding, timeout) is this hop's own
        for kv in md.iter() {
            match kv {
                KeyAndValueRef::Ascii(k, v)
                    if !k.as_str().starts_with("grpc-")
                        && !matches!(
                            k.as_str(),
                            "host" | "content-length" | "te" | CLIENT_IP_HEADER | ACTIVATOR_HEADER
                        ) =>
                {
                    req.metadata_mut().append(k.clone(), v.clone());
                }
                KeyAndValueRef::Binary(k, v) => {
                    req.metadata_mut().append_bin(k.clone(), v.clone());
                }
                _ => {}
            }
        }
        // the caller as this guard judged it, said by this zone's activator (whatever the client put there is gone)
        if let (Ok(ip), Ok(proof)) = (log.ip.to_string().parse(), self.proof.parse()) {
            req.metadata_mut().insert(CLIENT_IP_HEADER, ip);
            req.metadata_mut().insert(ACTIVATOR_HEADER, proof);
        }
        let mut client = McpClient::new(self.channel())
            .max_decoding_message_size(MAX_MESSAGE_BYTES)
            .max_encoding_message_size(MAX_MESSAGE_BYTES);
        match client.call(req).await {
            Ok(resp) => {
                log.emit(
                    cfg,
                    "ok",
                    Code::Ok,
                    json!({"forwarded": true, "woke": woke}),
                );
                Ok(tonic::Response::new(resp.into_inner()))
            }
            // nobody there (any more): the next call checks health again and wakes the zone if need be
            Err(st) if st.code() == Code::Unavailable => {
                self.mark_down();
                Err(log.deny(
                    cfg,
                    Status::unavailable("upstream unavailable, retry shortly"),
                    "upstream",
                ))
            }
            // the worker's own refusal (throttle, inflight, …) is the answer
            Err(st) => {
                log.emit(
                    cfg,
                    "denied",
                    st.code(),
                    json!({"forwarded": true, "woke": woke}),
                );
                Err(st)
            }
        }
    }

    async fn send(
        &self,
        client_ip: std::net::IpAddr,
        method: &Method,
        uri: &Uri,
        headers: &HeaderMap,
        body: &Bytes,
    ) -> Result<Response, hyper_util::client::legacy::Error> {
        let path = uri.path_and_query().map_or("/", |p| p.as_str());
        let mut req = axum::http::Request::builder()
            .method(method.clone())
            .uri(format!("{}{path}", self.upstream))
            .version(Version::HTTP_2)
            .body(Body::from(body.clone()))
            .expect("a request from parts that were one");
        for (k, v) in headers.iter().filter(|(k, _)| {
            !hop_by_hop(k) && !matches!(k.as_str(), CLIENT_IP_HEADER | ACTIVATOR_HEADER)
        }) {
            req.headers_mut().append(k.clone(), v.clone());
        }
        if let (Ok(ip), Ok(proof)) = (
            HeaderValue::from_str(&client_ip.to_string()),
            HeaderValue::from_str(&self.proof),
        ) {
            req.headers_mut().insert(CLIENT_IP_HEADER, ip);
            req.headers_mut().insert(ACTIVATOR_HEADER, proof);
        }
        let resp = self.http.request(req).await?;
        let (mut parts, incoming) = resp.into_parts();
        for k in [header::CONNECTION, header::TRANSFER_ENCODING] {
            parts.headers.remove(k);
        }
        // the body is handed on frame by frame: an SSE answer or a `subscriptions/listen` stream is never buffered
        Ok(Response::from_parts(parts, Body::new(incoming)))
    }

    /// The HTTP half: the request with all its headers and its body to the workers, their response streamed back.
    pub async fn http(
        self: &Arc<Self>,
        cfg: &Config,
        log: &CallLog,
        method: Method,
        uri: &Uri,
        headers: &HeaderMap,
        body: Bytes,
    ) -> Response {
        let mut woke = match self.ready().await {
            Ok(r) => matches!(r, Ready::Woke),
            Err(st) => return unavailable(&log.deny(cfg, st, "waking")),
        };
        let mut sent = self.send(log.ip, &method, uri, headers, &body).await;
        if sent.as_ref().is_err_and(|e| e.is_connect()) {
            // the workers left since the last health answer and nothing was sent: wake them, send once more
            self.mark_down();
            woke = match self.ready().await {
                Ok(_) => true,
                Err(st) => return unavailable(&log.deny(cfg, st, "waking")),
            };
            sent = self.send(log.ip, &method, uri, headers, &body).await;
        }
        match sent {
            Ok(resp) => {
                log.emit(
                    cfg,
                    "ok",
                    Code::Ok,
                    json!({"forwarded": true, "woke": woke, "upstream_status": resp.status().as_u16()}),
                );
                resp
            }
            Err(_) => {
                self.mark_down();
                unavailable(&log.deny(
                    cfg,
                    Status::unavailable("upstream unavailable, retry shortly"),
                    "upstream",
                ))
            }
        }
    }
}

/// rustls for the upstream: the install CA bundle only (an in-install peer), `h2` on ALPN, the peer's name.
fn upstream_tls(
    pem: &[u8],
    name: &str,
) -> Result<
    (
        tokio_rustls::TlsConnector,
        rustls_pki_types::ServerName<'static>,
    ),
    String,
> {
    use rustls_pki_types::CertificateDer;
    use rustls_pki_types::pem::PemObject;
    let mut roots = tokio_rustls::rustls::RootCertStore::empty();
    for cert in CertificateDer::pem_slice_iter(pem) {
        roots
            .add(cert.map_err(|_| "RAMEN_TLS_CA: not a PEM bundle".to_string())?)
            .map_err(|e| format!("RAMEN_TLS_CA: {e}"))?;
    }
    if roots.is_empty() {
        return Err("RAMEN_TLS_CA: no certificate in the bundle".into());
    }
    let mut config = tokio_rustls::rustls::ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth();
    config.alpn_protocols = vec![b"h2".to_vec()];
    let name = rustls_pki_types::ServerName::try_from(name.to_string())
        .map_err(|_| format!("RAMEN_TLS_SERVER_NAME {name:?} is not a host name"))?;
    Ok((tokio_rustls::TlsConnector::from(Arc::new(config)), name))
}

/// tonic's TLS channel to the workers, verifying `name` against the bundle `pem`.
fn tls_channel(endpoint: &Endpoint, pem: &[u8], name: &str) -> Result<Channel, String> {
    Ok(endpoint
        .clone()
        .tls_config(
            tonic::transport::ClientTlsConfig::new()
                .ca_certificate(tonic::transport::Certificate::from_pem(pem))
                .domain_name(name.to_string()),
        )
        .map_err(|e| format!("RAMEN_TLS_CA: {e}"))?
        .connect_lazy())
}

type Verifier = (
    tokio_rustls::TlsConnector,
    rustls_pki_types::ServerName<'static>,
);

/// §39.1: the install CA bundle the workers are verified with, re-read on use — a rotation rewrites the mounted file
/// (the two-CA bundle, later the new CA alone) and the activator follows without a restart.
pub struct Trust {
    path: std::path::PathBuf,
    name: String,
    state: Mutex<(Vec<u8>, Verifier)>,
}

impl Trust {
    fn load(path: &std::path::Path, name: &str) -> Result<Self, String> {
        let pem =
            std::fs::read(path).map_err(|e| format!("RAMEN_TLS_CA {}: {e}", path.display()))?;
        let verifier = upstream_tls(&pem, name)?;
        Ok(Self {
            path: path.to_path_buf(),
            name: name.to_string(),
            state: Mutex::new((pem, verifier)),
        })
    }

    /// The bundle and its verifier: a changed file is loaded; one that cannot be read or parsed keeps the last good.
    fn current(&self) -> (Vec<u8>, Verifier) {
        let mut st = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if let Ok(pem) = std::fs::read(&self.path)
            && pem != st.0
        {
            match upstream_tls(&pem, &self.name) {
                Ok(v) => {
                    *st = (pem, v);
                    emit(
                        "info",
                        "install CA bundle reloaded",
                        json!({"activator": true}),
                    );
                }
                Err(e) => emit(
                    "warn",
                    "install CA bundle reload failed; keeping the previous one",
                    json!({"activator": true, "error": e}),
                ),
            }
        }
        st.clone()
    }
}

/// The forwarding client's connector: TCP to the upstream (2 s), then TLS when configured.
#[derive(Clone)]
pub struct UpstreamConnector {
    tls: Option<Arc<Trust>>,
}

pub enum UpstreamIo {
    Plain(TokioIo<tokio::net::TcpStream>),
    Tls(Box<TokioIo<tokio_rustls::client::TlsStream<tokio::net::TcpStream>>>),
}

impl tower_service::Service<Uri> for UpstreamConnector {
    type Response = UpstreamIo;
    type Error = std::io::Error;
    type Future =
        std::pin::Pin<Box<dyn std::future::Future<Output = std::io::Result<UpstreamIo>> + Send>>;

    fn poll_ready(
        &mut self,
        _: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        std::task::Poll::Ready(Ok(()))
    }

    fn call(&mut self, uri: Uri) -> Self::Future {
        let tls = self.tls.as_ref().map(|t| t.current().1);
        Box::pin(async move {
            use std::io::{Error, ErrorKind};
            let host = uri
                .host()
                .ok_or_else(|| Error::new(ErrorKind::InvalidInput, "no host"))?
                .trim_matches(['[', ']'])
                .to_string();
            let port = uri
                .port_u16()
                .unwrap_or(if tls.is_some() { 443 } else { 80 });
            let tcp = tokio::time::timeout(
                HEALTH_TIMEOUT,
                tokio::net::TcpStream::connect((host.as_str(), port)),
            )
            .await
            .map_err(|_| Error::new(ErrorKind::TimedOut, "connect timed out"))??;
            tcp.set_nodelay(true)?;
            Ok(match tls {
                None => UpstreamIo::Plain(TokioIo::new(tcp)),
                Some((connector, name)) => {
                    UpstreamIo::Tls(Box::new(TokioIo::new(connector.connect(name, tcp).await?)))
                }
            })
        })
    }
}

impl Connection for UpstreamIo {
    fn connected(&self) -> Connected {
        match self {
            UpstreamIo::Plain(io) => io.inner().connected(),
            UpstreamIo::Tls(io) => {
                let (tcp, session) = io.inner().get_ref();
                let c = tcp.connected();
                if session.alpn_protocol() == Some(b"h2") {
                    c.negotiated_h2()
                } else {
                    c
                }
            }
        }
    }
}

impl hyper::rt::Read for UpstreamIo {
    fn poll_read(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        buf: hyper::rt::ReadBufCursor<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        match self.get_mut() {
            UpstreamIo::Plain(io) => std::pin::Pin::new(io).poll_read(cx, buf),
            UpstreamIo::Tls(io) => std::pin::Pin::new(io.as_mut()).poll_read(cx, buf),
        }
    }
}

impl hyper::rt::Write for UpstreamIo {
    fn poll_write(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        buf: &[u8],
    ) -> std::task::Poll<std::io::Result<usize>> {
        match self.get_mut() {
            UpstreamIo::Plain(io) => std::pin::Pin::new(io).poll_write(cx, buf),
            UpstreamIo::Tls(io) => std::pin::Pin::new(io.as_mut()).poll_write(cx, buf),
        }
    }

    fn poll_flush(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        match self.get_mut() {
            UpstreamIo::Plain(io) => std::pin::Pin::new(io).poll_flush(cx),
            UpstreamIo::Tls(io) => std::pin::Pin::new(io.as_mut()).poll_flush(cx),
        }
    }

    fn poll_shutdown(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        match self.get_mut() {
            UpstreamIo::Plain(io) => std::pin::Pin::new(io).poll_shutdown(cx),
            UpstreamIo::Tls(io) => std::pin::Pin::new(io.as_mut()).poll_shutdown(cx),
        }
    }
}

/// 503 with `Retry-After`: the caller's client retries, and by then the zone is up.
pub fn unavailable(st: &Status) -> Response {
    let mut resp = (
        StatusCode::SERVICE_UNAVAILABLE,
        axum::Json(json!({"error": st.message()})),
    )
        .into_response();
    resp.headers_mut().insert(
        header::RETRY_AFTER,
        HeaderValue::from_static(RETRY_AFTER_SECS),
    );
    resp
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn cfg(pairs: &[(&str, &str)]) -> Config {
        Config::from_map(
            &pairs
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect::<HashMap<_, _>>(),
        )
        .unwrap()
    }

    #[tokio::test]
    async fn the_upstream_is_plain_h2c_inside_the_cluster() {
        let a = Activator::new_for_test(&cfg(&[])).unwrap();
        assert_eq!(a.upstream, "http://worker-direct:8080");
        assert!(a.wake_url.is_none());
        let a = Activator::new_for_test(&cfg(&[
            ("RAMEN_ACTIVATOR_UPSTREAM", "http://w:9"),
            ("RAMEN_CONSOLE_INTERNAL_URL", "http://console:8000/"),
            ("RAMEN_GROUP", "demo"),
            ("RAMEN_ZONE", "a"),
        ]))
        .unwrap();
        assert_eq!(a.upstream, "http://w:9");
        assert_eq!(
            a.wake_url.as_deref(),
            Some("http://console:8000/api/v1/internal/zones/demo/a/wake")
        );
        assert!(
            Activator::new_for_test(&cfg(&[("RAMEN_ACTIVATOR_UPSTREAM", "https://w:9")])).is_err()
        );
        assert_eq!(
            a.metrics_json(7),
            json!({"asleep": true, "last_call_at": 7, "woken": 0})
        );
    }

    /// §39.3 (D86): with the install CA (or its own TLS) the activator reaches `worker-direct` over TLS, verified as
    /// `worker.ramen.internal` — gRPC health through tonic, a forwarded HTTP request through hyper; a peer the CA did
    /// not sign is never reached, and TLS without a CA to verify with is refused at start.
    #[tokio::test]
    async fn the_upstream_is_tls_under_the_install_ca() {
        let ca = crate::testca::TestCa::new();
        let dir = crate::testca::temp_dir("act");
        let (bundle, cert, key) = ca.write(&dir, &["worker.ramen.internal"]);
        let (bundle, cert, key) = (
            bundle.to_str().unwrap(),
            cert.to_str().unwrap(),
            key.to_str().unwrap(),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap().to_string();
        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        // the "workers": an activator is SERVING from the start, which is all this test needs of them
        let workers = cfg(&[
            ("RAMEN_ACTIVATOR", "1"),
            ("RAMEN_ACTIVATOR_UPSTREAM", "127.0.0.1:1"),
            ("RAMEN_TLS_CERT", cert),
            ("RAMEN_TLS_KEY", key),
            ("RAMEN_TLS_CA", bundle),
            ("RAMEN_BUCKET", dir.to_str().unwrap()),
        ]);
        let server = tokio::spawn(crate::server::run(workers, listener, async move {
            let _ = rx.await;
        }));
        let a = Activator::new_for_test(&cfg(&[
            ("RAMEN_ACTIVATOR_UPSTREAM", &addr),
            ("RAMEN_TLS_CA", bundle),
            ("RAMEN_WAKE_TIMEOUT_SECS", "2"),
        ]))
        .unwrap();
        assert_eq!(a.upstream, format!("https://{addr}"));
        a.poll_ms.store(50, Relaxed);
        assert!(matches!(a.ready().await, Ok(Ready::Up)));
        let resp = a
            .send(
                "10.0.0.1".parse().unwrap(),
                &Method::POST,
                &"/mcp".parse().unwrap(),
                &HeaderMap::new(),
                &Bytes::from_static(b"{}"),
            )
            .await
            .unwrap();
        assert!(resp.status().is_client_error(), "{}", resp.status());
        // another install's CA: the handshake fails, so the workers never look up
        let other = crate::testca::TestCa::new();
        let other_bundle = dir.join("other.crt");
        std::fs::write(&other_bundle, other.pem()).unwrap();
        let b = Activator::new_for_test(&cfg(&[
            ("RAMEN_ACTIVATOR_UPSTREAM", &addr),
            ("RAMEN_TLS_CA", other_bundle.to_str().unwrap()),
            ("RAMEN_WAKE_TIMEOUT_SECS", "1"),
        ]))
        .unwrap();
        b.poll_ms.store(50, Relaxed);
        assert!(b.ready().await.is_err());
        assert!(
            b.send(
                "10.0.0.1".parse().unwrap(),
                &Method::POST,
                &"/mcp".parse().unwrap(),
                &HeaderMap::new(),
                &Bytes::new(),
            )
            .await
            .is_err()
        );
        // §39.1: a rotation publishes the two-CA bundle into the same file — the running activator trusts it at once
        // (no restart): health through tonic and a forwarded request through hyper both reach the workers now
        std::fs::write(&other_bundle, format!("{}{}", other.pem(), ca.pem())).unwrap();
        assert!(b.ready().await.is_ok());
        assert!(
            b.send(
                "10.0.0.1".parse().unwrap(),
                &Method::POST,
                &"/mcp".parse().unwrap(),
                &HeaderMap::new(),
                &Bytes::from_static(b"{}"),
            )
            .await
            .is_ok()
        );
        // a broken bundle (a half-written file) keeps the last good one
        std::fs::write(&other_bundle, "not a bundle").unwrap();
        b.mark_down();
        assert!(b.ready().await.is_ok());
        // serving TLS itself (or an https:// upstream) without a bundle to verify the workers with: refused
        let e = Activator::new_for_test(&cfg(&[("RAMEN_TLS_CERT", cert), ("RAMEN_TLS_KEY", key)]))
            .err()
            .unwrap();
        assert!(e.contains("RAMEN_TLS_CA"), "{e}");
        tx.send(()).unwrap();
        server.await.unwrap();
    }

    #[tokio::test]
    async fn nobody_upstream_is_a_timeout_and_a_503_with_retry_after() {
        let a = Activator::new_for_test(&cfg(&[
            ("RAMEN_ACTIVATOR_UPSTREAM", "127.0.0.1:1"),
            ("RAMEN_WAKE_TIMEOUT_SECS", "1"),
        ]))
        .unwrap();
        a.poll_ms.store(50, Relaxed);
        let t0 = Instant::now();
        let st = a.ready().await.err().expect("nobody answers");
        assert_eq!(st.code(), Code::Unavailable);
        assert!(st.message().starts_with("waking"));
        assert!(
            t0.elapsed() >= Duration::from_millis(900) && t0.elapsed() < Duration::from_secs(8)
        );
        let resp = unavailable(&st);
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(resp.headers()[header::RETRY_AFTER], "10");
    }

    #[test]
    fn hop_headers_stay_on_their_hop() {
        for h in [
            "connection",
            "transfer-encoding",
            "host",
            "content-length",
            "te",
        ] {
            assert!(hop_by_hop(&header::HeaderName::from_static(h)), "{h}");
        }
        for h in [
            "authorization",
            "x-forwarded-for",
            "mcp-session-id",
            "mcp-protocol-version",
            "mcp-method",
            "mcp-name",
            "mcp-param-region",
            "traceparent",
            "accept",
            "content-type",
            "origin",
            "x-ramen-admin-key",
        ] {
            assert!(!hop_by_hop(&header::HeaderName::from_static(h)), "{h}");
        }
    }
}
