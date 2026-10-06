//! Streamable HTTP on the node's own port (CONTRACTS §16.1, D32).
//!
//! `POST /mcp` carries one JSON-RPC message; `GET /mcp` is `405` (no server-initiated messages); `DELETE /mcp`
//! ends a session; `OPTIONS /mcp` answers a browser preflight for an allowed origin. Every check is the SAME
//! function the gRPC service runs — `grpc::guard` and `grpc::dispatch_body` — fed the request headers as a
//! `MetadataMap`. This file adds only what HTTP has and gRPC does not: `Origin` validation and CORS,
//! `Accept`/`Content-Type` negotiation, `Mcp-Session-Id`, and the status mapping.
use crate::config::Config;
use crate::grpc::{self, Shared};
use crate::session::Invalid;
use crate::token;
use axum::Router;
use axum::extract::{Request, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use serde_json::{Value, json};
use std::net::SocketAddr;
use tonic::metadata::MetadataMap;
use tonic::{Code, Status};

pub const MCP_PATH: &str = "/mcp";
pub const PROTECTED_RESOURCE_PATH: &str = "/.well-known/oauth-protected-resource";
pub const SESSION_HEADER: &str = "mcp-session-id";
pub const PROTOCOL_HEADER: &str = "mcp-protocol-version";
/// What the node speaks (newest first). An unknown value on the request is `400`.
pub const PROTOCOL_VERSIONS: &[&str] = &["2025-06-18", "2025-03-26", "2024-11-05"];

pub fn router(app: Shared) -> Router {
    Router::new()
        .route(
            MCP_PATH,
            get(get_mcp)
                .post(post_mcp)
                .delete(delete_mcp)
                .options(options_mcp),
        )
        .route(PROTECTED_RESOURCE_PATH, get(protected_resource))
        .with_state(app)
}

/// The gRPC status of a denial, spelled the HTTP way (§16.1).
pub fn status_of(code: Code) -> StatusCode {
    match code {
        Code::Unauthenticated => StatusCode::UNAUTHORIZED,
        Code::PermissionDenied => StatusCode::FORBIDDEN,
        Code::ResourceExhausted => StatusCode::TOO_MANY_REQUESTS,
        Code::OutOfRange => StatusCode::PAYLOAD_TOO_LARGE,
        Code::Unavailable => StatusCode::SERVICE_UNAVAILABLE,
        Code::InvalidArgument => StatusCode::BAD_REQUEST,
        Code::NotFound => StatusCode::NOT_FOUND,
        _ => StatusCode::INTERNAL_SERVER_ERROR,
    }
}

fn peer_of(req: &Request) -> Option<SocketAddr> {
    use tonic::transport::server::{TcpConnectInfo, TlsConnectInfo};
    req.extensions()
        .get::<TcpConnectInfo>()
        .and_then(TcpConnectInfo::remote_addr)
        .or_else(|| {
            req.extensions()
                .get::<TlsConnectInfo<TcpConnectInfo>>()
                .and_then(|t| t.get_ref().remote_addr())
        })
}

/// A denial as an HTTP response. A `401` carries the RFC 9728 challenge when the worker has an authorization
/// server: absolute when the deployment told the worker its public base (§16.3), relative otherwise.
fn denied(st: &Status, cfg: &Config) -> Response {
    let code = status_of(st.code());
    let body = json!({"error": st.message()});
    let mut resp = (code, axum::Json(body)).into_response();
    if code == StatusCode::UNAUTHORIZED {
        let scope = token::resource(&cfg.group, &cfg.zone);
        let value = match (&cfg.oauth_issuer, &cfg.public_url) {
            (Some(_), Some(base)) => {
                format!(
                    "Bearer resource_metadata=\"{base}{PROTECTED_RESOURCE_PATH}\", scope=\"{scope}\""
                )
            }
            (Some(_), None) => {
                format!("Bearer resource_metadata=\"{PROTECTED_RESOURCE_PATH}\", scope=\"{scope}\"")
            }
            (None, _) => "Bearer".to_string(),
        };
        if let Ok(v) = HeaderValue::from_str(&value) {
            resp.headers_mut().insert(header::WWW_AUTHENTICATE, v);
        }
    }
    resp
}

fn accepts_json(headers: &HeaderMap) -> bool {
    match headers.get(header::ACCEPT).and_then(|v| v.to_str().ok()) {
        None => true,
        Some(a) => a
            .split(',')
            .map(|s| s.trim().split(';').next().unwrap_or("").trim())
            .any(|m| m == "application/json" || m == "*/*" || m == "application/*"),
    }
}

/// Whether the client can take an SSE answer (`Accept` names `text/event-stream`), the Streamable HTTP way of
/// carrying a server notification alongside a response.
fn accepts_sse(headers: &HeaderMap) -> bool {
    headers
        .get(header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|a| {
            a.split(',')
                .map(|s| s.trim().split(';').next().unwrap_or("").trim())
                .any(|m| m == "text/event-stream")
        })
}

/// One `data:` event per message, then the stream ends — exactly what a Streamable HTTP client reads from a POST
/// answered as `text/event-stream`.
fn sse(events: &[Value]) -> Response {
    let body: String = events.iter().map(|e| format!("data: {e}\n\n")).collect();
    let mut resp = (StatusCode::OK, body).into_response();
    let h = resp.headers_mut();
    h.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/event-stream"),
    );
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    resp
}

/// The first 12 hex of the manifest hash of the last load (C11), `""` before any load.
async fn manifest_hash12(app: &Shared) -> String {
    app.sidecar
        .loaded()
        .await
        .and_then(|l| {
            l.result["hash"]
                .as_str()
                .map(|h| h.chars().take(12).collect())
        })
        .unwrap_or_default()
}

fn is_json(headers: &HeaderMap) -> bool {
    headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .map(|c| {
            c.split(';')
                .next()
                .unwrap_or("")
                .trim()
                .eq_ignore_ascii_case("application/json")
        })
        .unwrap_or(false)
}

/// §16.1 Origin validation. No header → not a browser → fine. A header must match the allowlist exactly (or `*`);
/// a header that is present but not even ASCII is refused, not ignored (security review 0.5.0 L1).
fn origin_allowed(allowed: &[String], headers: &HeaderMap) -> bool {
    match headers.get(header::ORIGIN) {
        None => true,
        Some(v) => v.to_str().is_ok_and(|o| {
            allowed
                .iter()
                .any(|a| a == "*" || a.eq_ignore_ascii_case(o.trim_end_matches('/')))
        }),
    }
}

/// CORS for a browser at an allowed origin (L9): the exact origin echoed (never `*` with a credential header
/// in play), `Vary: Origin`, and the response headers a Streamable HTTP client reads.
fn cors(mut resp: Response, allowed: &[String], headers: &HeaderMap) -> Response {
    if let Some(o) = headers.get(header::ORIGIN)
        && origin_allowed(allowed, headers)
    {
        let h = resp.headers_mut();
        h.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, o.clone());
        h.insert(header::VARY, HeaderValue::from_static("Origin"));
        h.insert(
            header::ACCESS_CONTROL_EXPOSE_HEADERS,
            HeaderValue::from_static("Mcp-Session-Id, MCP-Protocol-Version, WWW-Authenticate"),
        );
    }
    resp
}

fn protocol_version(headers: &HeaderMap) -> Result<&'static str, ()> {
    match headers.get(PROTOCOL_HEADER).and_then(|v| v.to_str().ok()) {
        None => Ok(PROTOCOL_VERSIONS[0]),
        Some(v) => PROTOCOL_VERSIONS
            .iter()
            .copied()
            .find(|p| *p == v.trim())
            .ok_or(()),
    }
}

fn with_headers(mut resp: Response, version: &str, session: Option<&str>) -> Response {
    let h = resp.headers_mut();
    if let Ok(v) = HeaderValue::from_str(version) {
        h.insert(PROTOCOL_HEADER, v);
    }
    if let Some(s) = session.and_then(|s| HeaderValue::from_str(s).ok()) {
        h.insert(SESSION_HEADER, s);
    }
    resp
}

/// A log record for a request refused before the credential was read, so the denial is still logged.
fn log_for(cfg: &Config, peer: Option<SocketAddr>, md: &MetadataMap) -> grpc::CallLog {
    grpc::CallLog::new(crate::auth::client_ip(cfg, peer, md), "http")
}

/// Address first, then Origin (HTTP only), then the credential — the order every call goes through. A browser
/// from a foreign origin is refused even with a valid key (DNS rebinding), and before the credential is looked
/// at, so a refused page learns nothing about whether its key was any good.
fn pre_guard(
    cfg: &Config,
    peer: Option<SocketAddr>,
    md: &MetadataMap,
    headers: &HeaderMap,
) -> Option<Response> {
    let log = log_for(cfg, peer, md);
    if !crate::auth::ip_allowed(cfg, log.ip) {
        return Some(denied(
            &log.deny(cfg, Status::permission_denied("ip not allowed"), "cidr"),
            cfg,
        ));
    }
    if !origin_allowed(&cfg.allowed_origins, headers) {
        return Some(denied(
            &log.deny(
                cfg,
                Status::permission_denied("origin not allowed"),
                "origin",
            ),
            cfg,
        ));
    }
    None
}

async fn post_mcp(State(app): State<Shared>, req: Request) -> Response {
    let cfg = app.cfg.read().await.clone();
    let peer = peer_of(&req);
    let (parts, body) = req.into_parts();
    let headers = parts.headers;
    let md = MetadataMap::from_headers(headers.clone());

    let version = match protocol_version(&headers) {
        Ok(v) => v,
        Err(()) => {
            return (
                StatusCode::BAD_REQUEST,
                axum::Json(json!({"error": "unsupported MCP-Protocol-Version", "supported": PROTOCOL_VERSIONS})),
            )
                .into_response();
        }
    };
    if !accepts_json(&headers) {
        return (
            StatusCode::NOT_ACCEPTABLE,
            axum::Json(json!({"error": "Accept must include application/json"})),
        )
            .into_response();
    }
    if !is_json(&headers) {
        return (
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            axum::Json(json!({"error": "Content-Type must be application/json"})),
        )
            .into_response();
    }
    if let Some(resp) = pre_guard(&cfg, peer, &md, &headers) {
        return resp;
    }
    let (mut log, caller) = match grpc::guard(&app, &cfg, peer, &md, "http") {
        Ok(x) => x,
        Err(st) => return cors(denied(&st, &cfg), &cfg.allowed_origins, &headers),
    };
    let body = match axum::body::to_bytes(body, grpc::MAX_MESSAGE_BYTES).await {
        Ok(b) => b,
        Err(_) => {
            let st = Status::out_of_range("message too large");
            return denied(&log.deny(&cfg, st, "too_large"), &cfg);
        }
    };
    // §16.2: a session header on any request must validate for THIS credential, or the call is 404.
    let presented = headers
        .get(SESSION_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);
    let verified = match presented
        .as_deref()
        .map(|sid| app.sessions.verify(sid, &caller.binding))
    {
        None => None,
        Some(Ok(s)) => Some(s),
        Some(Err(why)) => {
            let (msg, reason) = match why {
                Invalid::Expired => ("session expired", "session_expired"),
                Invalid::BadMac | Invalid::Malformed => ("unknown session", "session_invalid"),
            };
            return with_headers(
                denied(&log.deny(&cfg, Status::not_found(msg), reason), &cfg),
                version,
                None,
            );
        }
    };
    let is_init = serde_json::from_slice::<Value>(&body)
        .ok()
        .and_then(|v| {
            v.get("method")
                .and_then(Value::as_str)
                .map(|m| m == "initialize")
        })
        .unwrap_or(false);
    let out = match grpc::dispatch_body(&app, &cfg, &mut log, &caller.kind, &body).await {
        Ok(o) => o,
        Err(st) => {
            return with_headers(
                cors(denied(&st, &cfg), &cfg.allowed_origins, &headers),
                version,
                None,
            );
        }
    };
    // C11: the manifest this pod serves right now; stamped into new session ids, compared with presented ones.
    let current12 = manifest_hash12(&app).await;
    let session = if is_init {
        Some(app.sessions.issue(&caller.binding, &current12))
    } else {
        presented
    };
    let resp = match out {
        Some(v) => {
            // C11: a session minted against another manifest learns of the change once per pod, on its first
            // response an SSE-capable client asks for after the rollout. A JSON-only client is answered as before.
            let stale = verified
                .as_ref()
                .filter(|s| !is_init && s.hash12 != current12 && accepts_sse(&headers))
                .is_some_and(|s| app.notified.first_time(&format!("{}.{current12}", s.nonce)));
            if stale {
                sse(&[
                    json!({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}),
                    v,
                ])
            } else {
                (StatusCode::OK, axum::Json(v)).into_response()
            }
        }
        None => StatusCode::ACCEPTED.into_response(),
    };
    cors(
        with_headers(resp, version, session.as_deref()),
        &cfg.allowed_origins,
        &headers,
    )
}

async fn get_mcp() -> Response {
    // No server-initiated messages: saying so with 405 is what the spec asks of a server that has none.
    let mut resp = (
        StatusCode::METHOD_NOT_ALLOWED,
        axum::Json(json!({"error": "no server-initiated messages; POST /mcp"})),
    )
        .into_response();
    resp.headers_mut().insert(
        header::ALLOW,
        HeaderValue::from_static("POST, DELETE, OPTIONS"),
    );
    resp
}

/// `OPTIONS /mcp`: a preflight is answered without a credential (browsers never send one on it), and only for
/// an allowed origin — an unlisted origin gets 403 with no CORS headers, so the browser stops there.
async fn options_mcp(State(app): State<Shared>, req: Request) -> Response {
    let cfg = app.cfg.read().await;
    if !origin_allowed(&cfg.allowed_origins, req.headers()) {
        return StatusCode::FORBIDDEN.into_response();
    }
    let mut resp = StatusCode::NO_CONTENT.into_response();
    let h = resp.headers_mut();
    h.insert(
        header::ACCESS_CONTROL_ALLOW_METHODS,
        HeaderValue::from_static("POST, DELETE, OPTIONS"),
    );
    h.insert(
        header::ACCESS_CONTROL_ALLOW_HEADERS,
        HeaderValue::from_static(
            "Authorization, Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version, ramen-group, ramen-zone",
        ),
    );
    h.insert(
        header::ACCESS_CONTROL_MAX_AGE,
        HeaderValue::from_static("600"),
    );
    cors(resp, &cfg.allowed_origins, req.headers())
}

async fn delete_mcp(State(app): State<Shared>, req: Request) -> Response {
    let cfg = app.cfg.read().await.clone();
    let peer = peer_of(&req);
    let md = MetadataMap::from_headers(req.headers().clone());
    if let Some(resp) = pre_guard(&cfg, peer, &md, req.headers()) {
        return resp;
    }
    let (log, caller) = match grpc::guard(&app, &cfg, peer, &md, "http") {
        Ok(x) => x,
        Err(st) => return denied(&st, &cfg),
    };
    let Some(sid) = req
        .headers()
        .get(SESSION_HEADER)
        .and_then(|v| v.to_str().ok())
    else {
        return (
            StatusCode::BAD_REQUEST,
            axum::Json(json!({"error": "Mcp-Session-Id required"})),
        )
            .into_response();
    };
    match app.sessions.verify(sid, &caller.binding) {
        Ok(_) => {
            log.emit(&cfg, "ok", Code::Ok, json!({"session": "ended"}));
            cors(
                StatusCode::NO_CONTENT.into_response(),
                &cfg.allowed_origins,
                req.headers(),
            )
        }
        Err(_) => denied(
            &log.deny(
                &cfg,
                Status::not_found("unknown session"),
                "session_invalid",
            ),
            &cfg,
        ),
    }
}

/// RFC 9728 (§16.3): unauthenticated, tells an OAuth client which authorization server to use for this worker.
/// `resource` is this endpoint's URL — RFC 9728 clients (Claude Code among them) refuse metadata whose
/// `resource` is not the URL they are talking to; the scope stays `mcp:<group>:<zone>`.
async fn protected_resource(State(app): State<Shared>, headers: HeaderMap) -> Response {
    let cfg = app.cfg.read().await;
    match &cfg.oauth_issuer {
        None => (
            StatusCode::NOT_FOUND,
            axum::Json(json!({"error": "no authorization server configured"})),
        )
            .into_response(),
        Some(issuer) => axum::Json(json!({
            "resource": format!("{}{MCP_PATH}", public_base(&cfg, &headers)),
            "authorization_servers": [issuer],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [token::resource(&cfg.group, &cfg.zone)],
        }))
        .into_response(),
    }
}

/// The base URL clients reach this worker at: `RAMEN_PUBLIC_URL` when the deploy set it, else what the request says.
fn public_base(cfg: &Config, headers: &HeaderMap) -> String {
    if let Some(base) = &cfg.public_url {
        return base.clone();
    }
    let hv = |n: &str| {
        headers
            .get(n)
            .and_then(|v| v.to_str().ok())
            .map(str::to_string)
    };
    let proto = hv("x-forwarded-proto").unwrap_or_else(|| "http".into());
    let host = hv("x-forwarded-host")
        .or_else(|| hv("host"))
        .unwrap_or_else(|| "localhost".into());
    format!("{proto}://{host}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use std::collections::HashMap;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::{TcpListener, TcpStream};

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
        m.entry("RAMEN_GROUP".into()).or_insert("demo".into());
        m.entry("RAMEN_ZONE".into()).or_insert("a".into());
        Config::from_map(&m).unwrap()
    }

    struct Reply {
        status: u16,
        headers: Vec<(String, String)>,
        body: String,
    }
    impl Reply {
        fn header(&self, name: &str) -> Option<&str> {
            self.headers
                .iter()
                .find(|(k, _)| k.eq_ignore_ascii_case(name))
                .map(|(_, v)| v.as_str())
        }
        fn json(&self) -> Value {
            serde_json::from_str(&self.body).unwrap_or(Value::Null)
        }
    }

    /// Raw HTTP/1.1 over a TCP socket: no client crate, nothing between the test and the wire.
    async fn http(
        addr: SocketAddr,
        method: &str,
        path: &str,
        headers: &[(&str, &str)],
        body: &str,
    ) -> Reply {
        let mut s = TcpStream::connect(addr).await.unwrap();
        let mut req = format!(
            "{method} {path} HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\nContent-Length: {}\r\n",
            body.len()
        );
        for (k, v) in headers {
            req.push_str(&format!("{k}: {v}\r\n"));
        }
        req.push_str("\r\n");
        req.push_str(body);
        s.write_all(req.as_bytes()).await.unwrap();
        let mut raw = Vec::new();
        s.read_to_end(&mut raw).await.unwrap();
        let text = String::from_utf8_lossy(&raw).to_string();
        let (head, body) = text.split_once("\r\n\r\n").unwrap_or((&text, ""));
        let mut lines = head.lines();
        let status = lines
            .next()
            .unwrap()
            .split_whitespace()
            .nth(1)
            .unwrap()
            .parse()
            .unwrap();
        let headers = lines
            .filter_map(|l| l.split_once(':'))
            .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
            .collect();
        Reply {
            status,
            headers,
            body: body.to_string(),
        }
    }

    async fn serve(c: Config) -> (SocketAddr, tokio::sync::oneshot::Sender<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        tokio::spawn(crate::server::run(c, listener, async move {
            let _ = rx.await;
        }));
        (addr, tx)
    }

    /// Like `serve`, but with an app the test keeps a handle on (to change what is "loaded" mid-test).
    async fn serve_app(app: Shared) -> (SocketAddr, tokio::sync::oneshot::Sender<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        let mut routes = grpc::routes(&app);
        let merged = std::mem::take(routes.axum_router_mut()).merge(router(app.clone()));
        *routes.axum_router_mut() = merged;
        tokio::spawn(async move {
            tonic::transport::Server::builder()
                .accept_http1(true)
                .add_routes(routes)
                .serve_with_incoming_shutdown(
                    tokio_stream::wrappers::TcpListenerStream::new(listener),
                    async {
                        let _ = rx.await;
                    },
                )
                .await
                .unwrap();
        });
        (addr, tx)
    }

    /// The `data:` payloads of an SSE body, in order.
    fn sse_events(body: &str) -> Vec<Value> {
        body.split("\n\n")
            .filter_map(|ev| ev.strip_prefix("data: "))
            .map(|d| serde_json::from_str(d).unwrap())
            .collect()
    }

    const JSON: (&str, &str) = ("Content-Type", "application/json");
    const KEY: (&str, &str) = ("Authorization", "Bearer rmk_test");
    const INIT: &str = r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}"#;
    const PING: &str = r#"{"jsonrpc":"2.0","id":2,"method":"ping"}"#;

    #[tokio::test]
    async fn initialize_over_http_returns_a_session_and_the_protocol_version() {
        let (addr, stop) = serve(cfg(&[
            ("RAMEN_MCP_KEYS", "rmk_test"),
            ("RAMEN_SESSION_SECRET", "s"),
        ]))
        .await;
        let r = http(addr, "POST", "/mcp", &[JSON, KEY], INIT).await;
        assert_eq!(r.status, 200, "{}", r.body);
        assert_eq!(r.json()["result"]["protocolVersion"], "2025-06-18");
        assert_eq!(r.header("mcp-protocol-version"), Some("2025-06-18"));
        let sid = r
            .header("mcp-session-id")
            .expect("session id on initialize")
            .to_string();
        // C11: four parts; nothing loaded yet, so the manifest stamp is empty
        assert_eq!(sid.split('.').count(), 4);
        assert_eq!(sid.split('.').nth(2), Some(""));
        assert_eq!(
            r.json()["result"]["capabilities"]["tools"]["listChanged"],
            true
        );
        // the id is accepted back on the next call under the same key, and echoed
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY, ("Mcp-Session-Id", &sid)],
            PING,
        )
        .await;
        assert_eq!(
            (r.status, r.header("mcp-session-id")),
            (200, Some(sid.as_str()))
        );
        // a notification is 202 with no body
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY],
            r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#,
        )
        .await;
        assert_eq!((r.status, r.body.as_str()), (202, ""));
        let _ = stop.send(());
    }

    #[tokio::test]
    async fn the_guards_are_the_grpc_guards_spelled_in_http() {
        let (addr, stop) = serve(cfg(&[
            ("RAMEN_MCP_KEYS", "rmk_test"),
            ("RAMEN_ALLOWED_CIDRS", "10.0.0.0/8"),
        ]))
        .await;
        // CIDR first: the loopback peer is outside 10/8, so even a good key is 403
        let r = http(addr, "POST", "/mcp", &[JSON, KEY], PING).await;
        assert_eq!(r.status, 403);
        let _ = stop.send(());

        let (addr, stop) = serve(cfg(&[("RAMEN_MCP_KEYS", "rmk_test")])).await;
        let r = http(addr, "POST", "/mcp", &[JSON], PING).await; // no key
        assert_eq!(r.status, 401);
        assert_eq!(r.header("www-authenticate"), Some("Bearer"));
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, ("Authorization", "Bearer rmk_wrong")],
            PING,
        )
        .await;
        assert_eq!(r.status, 401);
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, ("Authorization", "Basic rmk_test")],
            PING,
        )
        .await;
        assert_eq!(r.status, 401);
        // JSON-RPC protocol errors are 200 with a JSON-RPC error body, exactly as gRPC answers OK + error body
        let r = http(addr, "POST", "/mcp", &[JSON, KEY], "not json").await;
        assert_eq!(
            (r.status, r.json()["error"]["code"].as_i64()),
            (200, Some(-32700))
        );
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY],
            r#"{"jsonrpc":"2.0","id":9,"method":"nope"}"#,
        )
        .await;
        assert_eq!(
            (r.status, r.json()["error"]["code"].as_i64()),
            (200, Some(-32601))
        );
        let _ = stop.send(());
    }

    #[tokio::test]
    async fn no_keys_means_nobody_on_http_too() {
        let (addr, stop) = serve(cfg(&[])).await;
        assert_eq!(
            http(addr, "POST", "/mcp", &[JSON, KEY], PING).await.status,
            401
        );
        let _ = stop.send(());
    }

    #[tokio::test]
    async fn origin_is_validated_and_absent_is_fine() {
        let (addr, stop) = serve(cfg(&[("RAMEN_MCP_KEYS", "rmk_test")])).await;
        // default: no allowlist → any browser origin is refused, even with a valid key
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY, ("Origin", "https://evil.example")],
            PING,
        )
        .await;
        assert_eq!(r.status, 403, "{}", r.body);
        assert_eq!(
            http(addr, "POST", "/mcp", &[JSON, KEY], PING).await.status,
            200
        ); // non-browser client
        let _ = stop.send(());

        let (addr, stop) = serve(cfg(&[
            ("RAMEN_MCP_KEYS", "rmk_test"),
            ("RAMEN_ALLOWED_ORIGINS", "https://app.example"),
        ]))
        .await;
        assert_eq!(
            http(
                addr,
                "POST",
                "/mcp",
                &[JSON, KEY, ("Origin", "https://app.example")],
                PING
            )
            .await
            .status,
            200
        );
        assert_eq!(
            http(
                addr,
                "POST",
                "/mcp",
                &[JSON, KEY, ("Origin", "https://app.example.evil")],
                PING
            )
            .await
            .status,
            403
        );
        assert_eq!(
            http(
                addr,
                "POST",
                "/mcp",
                &[JSON, KEY, ("Origin", "http://app.example")],
                PING
            )
            .await
            .status,
            403
        ); // scheme matters
        let _ = stop.send(());

        let (addr, stop) = serve(cfg(&[
            ("RAMEN_MCP_KEYS", "rmk_test"),
            ("RAMEN_ALLOWED_ORIGINS", "*"),
        ]))
        .await;
        assert_eq!(
            http(
                addr,
                "POST",
                "/mcp",
                &[JSON, KEY, ("Origin", "http://localhost:3000")],
                PING
            )
            .await
            .status,
            200
        );
        let _ = stop.send(());
    }

    #[tokio::test]
    async fn a_session_cannot_be_reused_under_another_key_and_delete_ends_it() {
        let (addr, stop) = serve(cfg(&[
            ("RAMEN_MCP_KEYS", "rmk_test,rmk_other"),
            ("RAMEN_SESSION_SECRET", "s"),
        ]))
        .await;
        let sid = http(addr, "POST", "/mcp", &[JSON, KEY], INIT)
            .await
            .header("mcp-session-id")
            .unwrap()
            .to_string();
        let other = ("Authorization", "Bearer rmk_other");
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, other, ("Mcp-Session-Id", &sid)],
            PING,
        )
        .await;
        assert_eq!(
            r.status, 404,
            "another key must not ride a stolen session id (§16.2)"
        );
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY, ("Mcp-Session-Id", "garbage")],
            PING,
        )
        .await;
        assert_eq!(r.status, 404);
        // DELETE: needs the credential and the session
        assert_eq!(http(addr, "DELETE", "/mcp", &[KEY], "").await.status, 400);
        assert_eq!(
            http(addr, "DELETE", "/mcp", &[("Mcp-Session-Id", &sid)], "")
                .await
                .status,
            401
        );
        assert_eq!(
            http(
                addr,
                "DELETE",
                "/mcp",
                &[other, ("Mcp-Session-Id", &sid)],
                ""
            )
            .await
            .status,
            404
        );
        assert_eq!(
            http(addr, "DELETE", "/mcp", &[KEY, ("Mcp-Session-Id", &sid)], "")
                .await
                .status,
            204
        );
        let _ = stop.send(());
    }

    #[tokio::test]
    async fn content_negotiation_and_the_verbs_the_spec_names() {
        let (addr, stop) = serve(cfg(&[("RAMEN_MCP_KEYS", "rmk_test")])).await;
        assert_eq!(
            http(
                addr,
                "POST",
                "/mcp",
                &[JSON, KEY, ("Accept", "text/html")],
                PING
            )
            .await
            .status,
            406
        );
        assert_eq!(
            http(
                addr,
                "POST",
                "/mcp",
                &[JSON, KEY, ("Accept", "application/json, text/event-stream")],
                PING
            )
            .await
            .status,
            200
        );
        assert_eq!(
            http(
                addr,
                "POST",
                "/mcp",
                &[KEY, ("Content-Type", "text/plain")],
                PING
            )
            .await
            .status,
            415
        );
        assert_eq!(
            http(
                addr,
                "POST",
                "/mcp",
                &[JSON, KEY, ("MCP-Protocol-Version", "1999-01-01")],
                PING
            )
            .await
            .status,
            400
        );
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY, ("MCP-Protocol-Version", "2025-03-26")],
            PING,
        )
        .await;
        assert_eq!(
            (r.status, r.header("mcp-protocol-version")),
            (200, Some("2025-03-26"))
        );
        let r = http(addr, "GET", "/mcp", &[KEY], "").await;
        assert_eq!(
            (r.status, r.header("allow")),
            (405, Some("POST, DELETE, OPTIONS"))
        );
        // 4 MiB message limit, same number as the gRPC codec
        let big = format!(
            r#"{{"jsonrpc":"2.0","id":1,"method":"ping","params":{{"pad":"{}"}}}}"#,
            "x".repeat(grpc::MAX_MESSAGE_BYTES)
        );
        assert_eq!(
            http(addr, "POST", "/mcp", &[JSON, KEY], &big).await.status,
            413
        );
        let _ = stop.send(());
    }

    #[tokio::test]
    async fn a_console_token_is_a_credential_when_an_issuer_is_configured() {
        let secret = "zone-secret";
        let key = crate::session::Sessions::new(Some(secret), 60).derived_key("oauth");
        let mint = |aud: &str| {
            token::mint(
                &json!({"iss": "https://console.example", "sub": "u1", "aud": aud, "scope": aud, "exp": crate::session::now() + 60}),
                &key,
            )
        };
        let (addr, stop) = serve(cfg(&[
            ("RAMEN_MCP_KEYS", "rmk_test"),
            ("RAMEN_SESSION_SECRET", secret),
            ("RAMEN_OAUTH_ISSUER", "https://console.example"),
        ]))
        .await;
        let good = format!("Bearer {}", mint("mcp:demo:a"));
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, ("Authorization", &good)],
            INIT,
        )
        .await;
        assert_eq!(r.status, 200, "{}", r.body);
        let sid = r.header("mcp-session-id").unwrap().to_string();
        assert_eq!(
            http(
                addr,
                "POST",
                "/mcp",
                &[JSON, ("Authorization", &good), ("Mcp-Session-Id", &sid)],
                PING
            )
            .await
            .status,
            200
        );
        // a session minted for the user is not usable by a key, and vice versa
        assert_eq!(
            http(
                addr,
                "POST",
                "/mcp",
                &[JSON, KEY, ("Mcp-Session-Id", &sid)],
                PING
            )
            .await
            .status,
            404
        );
        let wrong_zone = format!("Bearer {}", mint("mcp:demo:b"));
        assert_eq!(
            http(
                addr,
                "POST",
                "/mcp",
                &[JSON, ("Authorization", &wrong_zone)],
                PING
            )
            .await
            .status,
            401
        );
        // the 401 now points OAuth clients at the resource metadata
        let r = http(addr, "POST", "/mcp", &[JSON], PING).await;
        assert!(
            r.header("www-authenticate")
                .unwrap()
                .contains("resource_metadata=\"/.well-known/oauth-protected-resource\"")
        );
        let r = http(addr, "GET", PROTECTED_RESOURCE_PATH, &[], "").await;
        assert_eq!(r.status, 200);
        // RFC 9728: `resource` is the URL the client talks to (no public base: taken from the request)
        assert_eq!(r.json()["resource"], format!("http://{addr}/mcp"));
        assert_eq!(r.json()["scopes_supported"][0], "mcp:demo:a");
        let r = http(
            addr,
            "GET",
            PROTECTED_RESOURCE_PATH,
            &[
                ("X-Forwarded-Proto", "https"),
                ("X-Forwarded-Host", "lb.example"),
            ],
            "",
        )
        .await;
        assert_eq!(r.json()["resource"], "https://lb.example/mcp");
        assert_eq!(
            r.json()["authorization_servers"][0],
            "https://console.example"
        );
        let _ = stop.send(());

        // without an issuer a JWT-shaped bearer is just a wrong key, and there is no resource metadata
        let (addr, stop) = serve(cfg(&[
            ("RAMEN_MCP_KEYS", "rmk_test"),
            ("RAMEN_SESSION_SECRET", secret),
        ]))
        .await;
        assert_eq!(
            http(
                addr,
                "POST",
                "/mcp",
                &[JSON, ("Authorization", &good)],
                PING
            )
            .await
            .status,
            401
        );
        assert_eq!(
            http(addr, "GET", PROTECTED_RESOURCE_PATH, &[], "")
                .await
                .status,
            404
        );
        let _ = stop.send(());
    }

    #[tokio::test]
    async fn grpc_still_serves_on_the_same_port() {
        let (addr, stop) = serve(cfg(&[("RAMEN_MCP_KEYS", "rmk_test")])).await;
        let ch = tonic::transport::Endpoint::from_shared(format!("http://{addr}"))
            .unwrap()
            .connect()
            .await
            .unwrap();
        let mut hc = tonic_health::pb::health_client::HealthClient::new(ch);
        let st = hc
            .check(tonic_health::pb::HealthCheckRequest {
                service: crate::grpc::ADMIN_SERVICE.into(),
            })
            .await
            .unwrap()
            .into_inner()
            .status;
        assert_eq!(
            st,
            tonic_health::pb::health_check_response::ServingStatus::Serving as i32
        );
        assert_eq!(
            http(addr, "POST", "/mcp", &[JSON, KEY], PING).await.status,
            200
        );
        let _ = stop.send(());
    }

    #[tokio::test]
    async fn audit_fixes_origin_fails_closed_cors_only_for_listed_origins_and_absolute_metadata() {
        let (addr, stop) = serve(cfg(&[
            ("RAMEN_MCP_KEYS", "rmk_test"),
            ("RAMEN_ALLOWED_ORIGINS", "https://app.example"),
            ("RAMEN_OAUTH_ISSUER", "https://console.example"),
            ("RAMEN_PUBLIC_URL", "https://mcp.example/"),
        ]))
        .await;
        // L1: a present-but-unparsable Origin is refused, not treated as absent
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY, ("Origin", "https://\u{e9}vil.example")],
            PING,
        )
        .await;
        assert_eq!(r.status, 403);
        // L9: preflight answered only for a listed origin; responses carry the exact origin, never `*`
        let pre = [
            ("Origin", "https://app.example"),
            ("Access-Control-Request-Method", "POST"),
        ];
        let r = http(addr, "OPTIONS", "/mcp", &pre, "").await;
        assert_eq!(
            (r.status, r.header("access-control-allow-origin")),
            (204, Some("https://app.example"))
        );
        assert!(
            r.header("access-control-allow-headers")
                .unwrap()
                .contains("Authorization")
        );
        let r = http(
            addr,
            "OPTIONS",
            "/mcp",
            &[("Origin", "https://evil.example")],
            "",
        )
        .await;
        assert_eq!(
            (r.status, r.header("access-control-allow-origin")),
            (403, None)
        );
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY, ("Origin", "https://app.example")],
            PING,
        )
        .await;
        assert_eq!(r.status, 200);
        assert_eq!(
            r.header("access-control-allow-origin"),
            Some("https://app.example")
        );
        assert_eq!(r.header("vary"), Some("Origin"));
        let r = http(addr, "POST", "/mcp", &[JSON, KEY], PING).await;
        assert_eq!(r.header("access-control-allow-origin"), None); // no Origin, no CORS headers
        // L8: the challenge names an absolute metadata URL when the worker knows its public base
        let r = http(addr, "POST", "/mcp", &[JSON], PING).await;
        assert_eq!(
            r.header("www-authenticate"),
            Some(
                "Bearer resource_metadata=\"https://mcp.example/.well-known/oauth-protected-resource\", scope=\"mcp:demo:a\""
            )
        );
        // ...and the metadata's `resource` is that base plus the MCP path, whatever Host the request carries
        let r = http(
            addr,
            "GET",
            PROTECTED_RESOURCE_PATH,
            &[("Host", "other")],
            "",
        )
        .await;
        assert_eq!(r.json()["resource"], "https://mcp.example/mcp");
        let _ = stop.send(());

        // L2: a foreign origin is refused before the credential is looked at (a bad key gets the same 403)
        let (addr, stop) = serve(cfg(&[("RAMEN_MCP_KEYS", "rmk_test")])).await;
        let bad = [
            JSON,
            ("Authorization", "Bearer rmk_wrong"),
            ("Origin", "https://evil.example"),
        ];
        assert_eq!(http(addr, "POST", "/mcp", &bad, PING).await.status, 403);
        let _ = stop.send(());
    }

    /// C11 (0.7.2): a session carries the manifest hash it was initialized against; after a rollout the first
    /// response an SSE-capable client asks for arrives as SSE with `notifications/tools/list_changed` first, once
    /// per session per pod; JSON-only clients and 0.7.1 three-part ids are served as before.
    #[tokio::test]
    async fn a_rollout_is_announced_once_per_session_to_sse_capable_clients() {
        let app = grpc::app(cfg(&[
            ("RAMEN_MCP_KEYS", "rmk_test"),
            ("RAMEN_SESSION_SECRET", "s"),
        ]))
        .await;
        let set_hash = |h: char| {
            let app = app.clone();
            async move {
                app.sidecar
                    .set_loaded_for_test(json!({"tools": [], "hash": h.to_string().repeat(64)}))
                    .await;
            }
        };
        set_hash('a').await;
        let (addr, stop) = serve_app(app.clone()).await;
        const BOTH: (&str, &str) = ("Accept", "application/json, text/event-stream");
        let r = http(addr, "POST", "/mcp", &[JSON, KEY, BOTH], INIT).await;
        assert_eq!(r.status, 200, "{}", r.body);
        let sid = r.header("mcp-session-id").unwrap().to_string();
        assert_eq!(sid.split('.').nth(2), Some("aaaaaaaaaaaa"));
        // same manifest: plain JSON even though the client could take SSE
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY, BOTH, ("Mcp-Session-Id", &sid)],
            PING,
        )
        .await;
        assert_eq!(r.status, 200);
        assert!(
            r.header("content-type")
                .unwrap()
                .starts_with("application/json")
        );
        // rollout: the manifest changes under the session
        set_hash('b').await;
        // a notification (202, no response) is not the place for it, and does not use up the one announcement
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY, BOTH, ("Mcp-Session-Id", &sid)],
            r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#,
        )
        .await;
        assert_eq!((r.status, r.body.as_str()), (202, ""));
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY, BOTH, ("Mcp-Session-Id", &sid)],
            PING,
        )
        .await;
        assert_eq!(r.status, 200, "{}", r.body);
        assert_eq!(r.header("content-type"), Some("text/event-stream"));
        assert_eq!(r.header("mcp-session-id"), Some(sid.as_str()));
        let events = sse_events(&r.body);
        assert_eq!(events.len(), 2, "{}", r.body);
        assert_eq!(
            events[0],
            json!({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        );
        assert_eq!(events[1], json!({"jsonrpc": "2.0", "id": 2, "result": {}}));
        assert!(
            r.body.starts_with("data: ") && r.body.ends_with("\n\n"),
            "{:?}",
            r.body
        );
        // once per session per pod: the next request is plain JSON again, even though the stamp still differs
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY, BOTH, ("Mcp-Session-Id", &sid)],
            PING,
        )
        .await;
        assert!(
            r.header("content-type")
                .unwrap()
                .starts_with("application/json")
        );
        assert_eq!(r.json()["result"], json!({}));
        // a second rollout is announced again (the set is keyed by nonce and manifest)
        set_hash('c').await;
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY, BOTH, ("Mcp-Session-Id", &sid)],
            PING,
        )
        .await;
        assert_eq!(r.header("content-type"), Some("text/event-stream"));
        assert_eq!(sse_events(&r.body).len(), 2);
        // a fresh initialize is stamped with the current manifest and is not notified
        let r = http(addr, "POST", "/mcp", &[JSON, KEY, BOTH], INIT).await;
        let fresh = r.header("mcp-session-id").unwrap().to_string();
        assert_eq!(fresh.split('.').nth(2), Some("cccccccccccc"));
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY, BOTH, ("Mcp-Session-Id", &fresh)],
            PING,
        )
        .await;
        assert!(
            r.header("content-type")
                .unwrap()
                .starts_with("application/json")
        );
        // a JSON-only client never sees SSE, and does not use up the announcement for an SSE-capable retry
        set_hash('d').await;
        for accept in [None, Some(("Accept", "application/json"))] {
            let mut hs = vec![JSON, KEY, ("Mcp-Session-Id", &fresh)];
            hs.extend(accept);
            let r = http(addr, "POST", "/mcp", &hs, PING).await;
            assert_eq!(r.status, 200);
            assert!(
                r.header("content-type")
                    .unwrap()
                    .starts_with("application/json")
            );
            assert_eq!(r.json()["result"], json!({}));
        }
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY, BOTH, ("Mcp-Session-Id", &fresh)],
            PING,
        )
        .await;
        assert_eq!(r.header("content-type"), Some("text/event-stream"));
        // a 0.7.1 three-part id is still a session (stamp ""), so it is told about the current manifest once
        let d = ring::digest::digest(&ring::digest::SHA256, b"rmk_test");
        let binding: String = d.as_ref().iter().map(|b| format!("{b:02x}")).collect();
        let old = app.sessions.issue_legacy(&binding);
        assert_eq!(old.split('.').count(), 3);
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY, BOTH, ("Mcp-Session-Id", &old)],
            PING,
        )
        .await;
        assert_eq!(
            (r.status, r.header("mcp-session-id")),
            (200, Some(old.as_str()))
        );
        assert_eq!(r.header("content-type"), Some("text/event-stream"));
        assert_eq!(sse_events(&r.body)[1]["result"], json!({}));
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, KEY, BOTH, ("Mcp-Session-Id", &old)],
            PING,
        )
        .await;
        assert!(
            r.header("content-type")
                .unwrap()
                .starts_with("application/json")
        );
        assert_eq!(
            http(addr, "DELETE", "/mcp", &[KEY, ("Mcp-Session-Id", &old)], "")
                .await
                .status,
            204
        );
        let _ = stop.send(());
    }

    #[test]
    fn status_mapping_is_the_contract_table() {
        assert_eq!(status_of(Code::Unauthenticated), StatusCode::UNAUTHORIZED);
        assert_eq!(status_of(Code::PermissionDenied), StatusCode::FORBIDDEN);
        assert_eq!(
            status_of(Code::ResourceExhausted),
            StatusCode::TOO_MANY_REQUESTS
        );
        assert_eq!(status_of(Code::OutOfRange), StatusCode::PAYLOAD_TOO_LARGE);
        assert_eq!(
            status_of(Code::Unavailable),
            StatusCode::SERVICE_UNAVAILABLE
        );
        assert_eq!(status_of(Code::Internal), StatusCode::INTERNAL_SERVER_ERROR);
    }

    fn user_token(secret: &str, sub: &str, exp: u64, jti: &str) -> String {
        let key = crate::session::Sessions::new(Some(secret), 60).derived_key("oauth");
        format!(
            "Bearer {}",
            token::mint(
                &json!({"iss": "https://console.example", "sub": sub, "aud": "mcp:demo:a", "scope": "mcp:demo:a", "exp": exp, "jti": jti}),
                &key,
            )
        )
    }

    /// A2 (0.7.0): a session is bound to the person (`user:<sub>`), never to one access token, so a token
    /// refreshed mid-task keeps the MCP session; another person's token does not.
    #[tokio::test]
    async fn a_refreshed_token_for_the_same_subject_keeps_the_session() {
        let secret = "zone-secret";
        let (addr, stop) = serve(cfg(&[
            ("RAMEN_SESSION_SECRET", secret),
            ("RAMEN_OAUTH_ISSUER", "https://console.example"),
        ]))
        .await;
        let now = crate::session::now();
        let first = user_token(secret, "u1", now + 60, "j1");
        let refreshed = user_token(secret, "u1", now + 3600, "j2");
        assert_ne!(first, refreshed);
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, ("Authorization", &first)],
            INIT,
        )
        .await;
        assert_eq!(r.status, 200, "{}", r.body);
        let sid = r.header("mcp-session-id").unwrap().to_string();
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[
                JSON,
                ("Authorization", &refreshed),
                ("Mcp-Session-Id", &sid),
            ],
            PING,
        )
        .await;
        assert_eq!(
            (r.status, r.header("mcp-session-id")),
            (200, Some(sid.as_str()))
        );
        let other = user_token(secret, "u2", now + 3600, "j3");
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, ("Authorization", &other), ("Mcp-Session-Id", &sid)],
            PING,
        )
        .await;
        assert_eq!(
            r.status, 404,
            "another person's token must not ride the session"
        );
        let _ = stop.send(());
    }

    /// A4 (0.7.0): an expired token is answered exactly like no token at all — 401 with the RFC 9728
    /// challenge — so an OAuth client restarts sign-in instead of giving up.
    #[tokio::test]
    async fn an_expired_token_gets_the_same_401_challenge_as_no_token() {
        let secret = "zone-secret";
        let (addr, stop) = serve(cfg(&[
            ("RAMEN_SESSION_SECRET", secret),
            ("RAMEN_OAUTH_ISSUER", "https://console.example"),
            ("RAMEN_PUBLIC_URL", "https://mcp.example"),
        ]))
        .await;
        let expired = user_token(secret, "u1", crate::session::now() - 1, "j1");
        let none = http(addr, "POST", "/mcp", &[JSON], PING).await;
        let r = http(
            addr,
            "POST",
            "/mcp",
            &[JSON, ("Authorization", &expired)],
            PING,
        )
        .await;
        assert_eq!((none.status, r.status), (401, 401));
        assert_eq!(
            r.header("www-authenticate"),
            Some(
                "Bearer resource_metadata=\"https://mcp.example/.well-known/oauth-protected-resource\", scope=\"mcp:demo:a\""
            )
        );
        assert_eq!(
            r.header("www-authenticate"),
            none.header("www-authenticate")
        );
        assert_eq!(r.body, none.body); // nothing tells the caller which check failed (token.rs)
        let _ = stop.send(());
    }
}
