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
use hyper_util::client::legacy::connect::HttpConnector;
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

pub struct Activator {
    group: String,
    zone: String,
    wake_url: Option<String>,
    admin_key: Option<String>,
    timeout: Duration,
    /// `grpc::activator_proof`: sent with every forwarded call so the workers take the client address from us.
    proof: String,
    /// `http://host:port` of the zone's real workers (`Service/worker-direct`), h2c.
    upstream: String,
    channel: Channel,
    http: Client<HttpConnector, Body>,
    wake: AtomicU8,
    polling: AtomicBool,
    /// Counts SERVING observations by the poller; waiting calls watch it change.
    up: watch::Sender<u64>,
    up_at: Mutex<Option<Instant>>,
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
        let upstream = match cfg.activator_upstream.as_str() {
            u if u.starts_with("http://") => u.to_string(),
            u if u.contains("://") => {
                return Err(
                    "RAMEN_ACTIVATOR_UPSTREAM must be host:port or http://host:port".into(),
                );
            }
            u => format!("http://{u}"),
        };
        let channel = Endpoint::from_shared(upstream.clone())
            .map_err(|e| format!("RAMEN_ACTIVATOR_UPSTREAM: {e}"))?
            .connect_timeout(HEALTH_TIMEOUT)
            .connect_lazy();
        let mut connector = HttpConnector::new();
        connector.set_connect_timeout(Some(HEALTH_TIMEOUT));
        connector.set_nodelay(true);
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
            timeout: Duration::from_secs(cfg.wake_timeout_secs),
            proof,
            upstream,
            channel,
            http,
            wake: AtomicU8::new(IDLE),
            polling: AtomicBool::new(false),
            up: watch::channel(0).0,
            up_at: Mutex::new(None),
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
        let mut client = HealthClient::new(self.channel.clone());
        let check = client.check(HealthCheckRequest {
            service: String::new(),
        });
        matches!(
            tokio::time::timeout(HEALTH_TIMEOUT, check).await,
            Ok(Ok(r)) if r.get_ref().status == ServingStatus::Serving as i32
        )
    }

    fn mark_up(&self) {
        *self.up_at.lock().unwrap_or_else(|e| e.into_inner()) = Some(Instant::now());
        // this wake is over: the next time the workers are gone, the console is asked again
        self.wake.store(IDLE, Relaxed);
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
        if self
            .wake
            .compare_exchange(IDLE, POSTING, Relaxed, Relaxed)
            .is_err()
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
            match crate::netc::post_json(url, &headers, body, me.timeout).await {
                Ok((status, _)) if (200..300).contains(&status) => {
                    me.woken.fetch_add(1, Relaxed);
                    // IDLE again if the workers came up meanwhile (`mark_up`); otherwise this wake is asked for
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
        if self.fresh() {
            return Ok(Ready::Up);
        }
        if self.serving().await {
            self.mark_up();
            return Ok(Ready::Up);
        }
        self.mark_down();
        let mut up = self.up.subscribe();
        self.request_wake();
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
        let mut client = McpClient::new(self.channel.clone())
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
