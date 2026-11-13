//! CONTRACTS §24 over the real transports: the activator in front of a sleeping zone (§24.1), the telemetry consent
//! gate and the OTLP export (§24.3). Same shape as `tests/modern.rs`: real listeners, the stand-in runtime of
//! `tests/fake_runtime`, raw HTTP/1.1 over a socket, and stand-in servers for the console and the collector.
use axum::extract::State;
use ramen_node::config::Config;
use ramen_node::grpc::{Shared, app_with, routes};
use ramen_node::pb::admin_client::AdminClient;
use ramen_node::pb::mcp_client::McpClient;
use ramen_node::pb::{JsonRpc, MetricsRequest, ReloadRequest};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::Ordering::Relaxed;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

const V: &str = "2026-07-28";
const KEY: &str = "ramen_telemetry_consent";
const MCP_KEY: &str = "rmk_live_0123456789abcdef";
const ADMIN_KEY: &str = "adm-s3cr3t-9f8e7d";

struct Node {
    app: Shared,
    addr: SocketAddr,
    _stop: tokio::sync::oneshot::Sender<()>,
}

fn config(extra: &[(&str, &str)]) -> Config {
    let fake = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fake_runtime");
    let nonce: String = ramen_node::session::random(6)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    let bucket = std::env::temp_dir().join(format!("ramen-sleep-{nonce}"));
    std::fs::create_dir_all(&bucket).unwrap();
    let keys = format!("{MCP_KEY},k2");
    let mut m: HashMap<String, String> = [
        ("RAMEN_MCP_KEYS", keys.as_str()),
        ("RAMEN_ADMIN_KEY", ADMIN_KEY),
        ("RAMEN_GROUP", "demo"),
        ("RAMEN_ZONE", "a"),
        ("RAMEN_SESSION_SECRET", "s"),
        ("RAMEN_CALL_TIMEOUT_SECS", "20"),
    ]
    .iter()
    .map(|(k, v)| (k.to_string(), v.to_string()))
    .collect();
    m.insert(
        "RAMEN_PYTHON".into(),
        std::env::var("RAMEN_TEST_PYTHON").unwrap_or_else(|_| "python3".into()),
    );
    m.insert("RAMEN_PYTHONPATH".into(), fake.display().to_string());
    m.insert("RAMEN_BUCKET".into(), bucket.display().to_string());
    m.extend(extra.iter().map(|(k, v)| (k.to_string(), v.to_string())));
    Config::from_map(&m).unwrap()
}

/// A node (worker with the fake runtime loaded, or an activator) serving on `listener`.
async fn serve(cfg: Config, listener: TcpListener) -> Node {
    let again = cfg.clone();
    serve_from(cfg, Box::new(move || Ok(again.clone())), listener).await
}

/// Like `serve`, with what an `Admin/Reload` re-reads (§26.3: the console pushes a new telemetry config).
async fn serve_from(
    cfg: Config,
    source: ramen_node::grpc::ConfigSource,
    listener: TcpListener,
) -> Node {
    let activator = cfg.activator;
    let app = app_with(cfg, source).await;
    if !activator {
        app.load().await.expect("the fake runtime loads");
    }
    let addr = listener.local_addr().unwrap();
    let (tx, rx) = tokio::sync::oneshot::channel::<()>();
    let mut r = routes(&app);
    let merged = std::mem::take(r.axum_router_mut()).merge(ramen_node::http::router(app.clone()));
    *r.axum_router_mut() = merged;
    tokio::spawn(async move {
        tonic::transport::Server::builder()
            .accept_http1(true)
            .add_routes(r)
            .serve_with_incoming_shutdown(
                tokio_stream::wrappers::TcpListenerStream::new(listener),
                async {
                    let _ = rx.await;
                },
            )
            .await
            .unwrap();
    });
    Node {
        app,
        addr,
        _stop: tx,
    }
}

async fn worker(extra: &[(&str, &str)]) -> Node {
    serve(
        config(extra),
        TcpListener::bind("127.0.0.1:0").await.unwrap(),
    )
    .await
}

/// What a stand-in server (the console, a collector) was sent: path, headers, body.
type Seen = Arc<Mutex<Vec<(String, axum::http::HeaderMap, String)>>>;

/// Records every request, answers `status`, and tells `woken` about each one.
async fn recorder(status: u16) -> (SocketAddr, Seen, Arc<tokio::sync::Notify>) {
    let seen: Seen = Default::default();
    let hit = Arc::new(tokio::sync::Notify::new());
    type S = (Seen, u16, Arc<tokio::sync::Notify>);
    let app = axum::Router::new()
        .fallback(
            |State((seen, status, hit)): State<S>, req: axum::extract::Request| async move {
                let (parts, body) = req.into_parts();
                let body = axum::body::to_bytes(body, 8 << 20)
                    .await
                    .unwrap_or_default();
                seen.lock().unwrap().push((
                    parts.uri.path().to_string(),
                    parts.headers,
                    String::from_utf8_lossy(&body).to_string(),
                ));
                hit.notify_one();
                (axum::http::StatusCode::from_u16(status).unwrap(), "{}")
            },
        )
        .with_state((seen.clone(), status, hit.clone()));
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await });
    (addr, seen, hit)
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
    /// The JSON-RPC message: the body, or the last `data:` event of an SSE answer (chunk framing falls away).
    fn json(&self) -> Value {
        serde_json::from_str(&self.body)
            .ok()
            .or_else(|| self.events().pop())
            .or_else(|| {
                self.body
                    .lines()
                    .filter_map(|l| serde_json::from_str(l.trim()).ok())
                    .next_back()
            })
            .unwrap_or(Value::Null)
    }
    fn events(&self) -> Vec<Value> {
        self.body
            .lines()
            .filter_map(|l| l.strip_prefix("data: "))
            .filter_map(|d| serde_json::from_str(d.trim()).ok())
            .collect()
    }
    fn code(&self) -> Option<i64> {
        self.json()["error"]["code"].as_i64()
    }
    fn text(&self) -> String {
        self.json()["result"]["content"][0]["text"]
            .as_str()
            .unwrap_or("")
            .to_string()
    }
}

fn request(addr: SocketAddr, headers: &[(&str, &str)], body: &str) -> String {
    let mut req = format!(
        "POST /mcp HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n",
        body.len()
    );
    for (k, v) in headers {
        req.push_str(&format!("{k}: {v}\r\n"));
    }
    req.push_str("\r\n");
    req.push_str(body);
    req
}

/// One POST /mcp, read to the end of the response.
async fn post(addr: SocketAddr, headers: &[(&str, &str)], body: &str) -> Reply {
    let mut s = TcpStream::connect(addr).await.unwrap();
    s.write_all(request(addr, headers, body).as_bytes())
        .await
        .unwrap();
    let mut raw = Vec::new();
    s.read_to_end(&mut raw).await.unwrap();
    let text = String::from_utf8_lossy(&raw).to_string();
    let (h, b) = text.split_once("\r\n\r\n").unwrap_or((&text, ""));
    let mut lines = h.lines();
    let status = lines
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let headers = lines
        .filter_map(|l| l.split_once(':'))
        .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
        .collect();
    Reply {
        status,
        headers,
        body: b.to_string(),
    }
}

fn meta(caps: Value) -> Value {
    json!({"io.modelcontextprotocol/protocolVersion": V, "io.modelcontextprotocol/clientCapabilities": caps})
}

/// A well-formed 2026-07-28 request with `bearer`, declaring `caps`.
async fn modern(
    addr: SocketAddr,
    bearer: &str,
    caps: Value,
    method: &str,
    mut params: Value,
    extra: &[(&str, &str)],
) -> Reply {
    let mut m = meta(caps);
    if let Some(own) = params.get("_meta").and_then(Value::as_object) {
        m.as_object_mut().unwrap().extend(own.clone());
    }
    params["_meta"] = m;
    let auth = format!("Bearer {bearer}");
    let name = match method {
        "tools/call" | "prompts/get" => params["name"].as_str(),
        "resources/read" => params["uri"].as_str(),
        _ => None,
    }
    .map(String::from);
    let mut hs = vec![
        ("Authorization", auth.as_str()),
        ("MCP-Protocol-Version", V),
        ("Mcp-Method", method),
    ];
    if let Some(n) = &name {
        hs.push(("Mcp-Name", n));
    }
    hs.extend_from_slice(extra);
    let body = json!({"jsonrpc": "2.0", "id": 7, "method": method, "params": params});
    post(addr, &hs, &body.to_string()).await
}

async fn legacy(addr: SocketAddr, bearer: &str, method: &str, params: Value) -> Reply {
    let body = json!({"jsonrpc": "2.0", "id": 3, "method": method, "params": params});
    let auth = format!("Bearer {bearer}");
    post(addr, &[("Authorization", &auth)], &body.to_string()).await
}

async fn grpc_call(
    addr: SocketAddr,
    metadata: &[(&'static str, &str)],
    body: Value,
) -> Result<Value, tonic::Status> {
    let mut c = McpClient::connect(format!("http://{addr}")).await.unwrap();
    let mut req = tonic::Request::new(JsonRpc {
        body: body.to_string().into_bytes(),
    });
    for (k, v) in metadata {
        req.metadata_mut().insert(*k, v.parse().unwrap());
    }
    let out = c.call(req).await?.into_inner().body;
    Ok(serde_json::from_slice(&out).unwrap_or(Value::Null))
}

async fn admin_metrics(addr: SocketAddr) -> Value {
    let mut c = AdminClient::connect(format!("http://{addr}"))
        .await
        .unwrap();
    let mut req = tonic::Request::new(MetricsRequest {});
    req.metadata_mut()
        .insert("x-ramen-admin-key", ADMIN_KEY.parse().unwrap());
    serde_json::from_slice(&c.metrics(req).await.unwrap().into_inner().json).unwrap()
}

/// All tests share one process and so one log file (the log sink is process-global); lines are found by content.
fn log_path() -> PathBuf {
    static PATH: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();
    PATH.get_or_init(|| {
        let p = std::env::temp_dir().join(format!("ramen-sleep-{}.log", std::process::id()));
        ramen_node::log::set_file(Some(p.clone()));
        p
    })
    .clone()
}

fn log_line(pred: impl Fn(&Value) -> bool) -> Option<Value> {
    std::fs::read_to_string(log_path())
        .unwrap_or_default()
        .lines()
        .rev()
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .find(|v| v["msg"] == "mcp" && pred(v))
}

// ---------------------------------------------------------------------------------------------- §24.1 metrics

/// `last_call_at` moves only for an authenticated, accepted MCP call — on either transport.
#[tokio::test]
async fn last_call_at_counts_accepted_mcp_calls_only() {
    let n = worker(&[]).await;
    let m = admin_metrics(n.addr).await;
    assert_eq!(m["last_call_at"], 0, "{m}");
    for k in ["inflight", "tasks", "subscriptions"] {
        assert_eq!(m[k], 0, "{k} sits next to last_call_at: {m}");
    }
    // a refused credential, a health check and Admin/Metrics itself do not count
    let r = legacy(n.addr, "rmk_wrong", "tools/list", json!({})).await;
    assert_eq!(r.status, 401);
    let denied = grpc_call(
        n.addr,
        &[],
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
    )
    .await;
    assert_eq!(denied.unwrap_err().code(), tonic::Code::Unauthenticated);
    let ch = tonic::transport::Endpoint::from_shared(format!("http://{}", n.addr))
        .unwrap()
        .connect()
        .await
        .unwrap();
    tonic_health::pb::health_client::HealthClient::new(ch)
        .check(tonic_health::pb::HealthCheckRequest {
            service: String::new(),
        })
        .await
        .unwrap();
    assert_eq!(admin_metrics(n.addr).await["last_call_at"], 0);
    // an accepted call over HTTP…
    let before = ramen_node::session::now();
    let r = legacy(n.addr, MCP_KEY, "tools/list", json!({})).await;
    assert_eq!(r.status, 200, "{}", r.body);
    let at = admin_metrics(n.addr).await["last_call_at"]
        .as_u64()
        .unwrap();
    assert!(at >= before && at <= before + 5, "{at} vs {before}");
    // …and over gRPC
    n.app.metrics.last_call_at.store(0, Relaxed);
    let auth = format!("Bearer {MCP_KEY}");
    grpc_call(
        n.addr,
        &[("authorization", &auth)],
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
    )
    .await
    .unwrap();
    assert!(
        admin_metrics(n.addr).await["last_call_at"]
            .as_u64()
            .unwrap()
            >= before
    );
}

// -------------------------------------------------------------------------------------------- §24.1 activator

struct Zone {
    activator: Node,
    console: Seen,
    /// Resolves to the worker once the console was asked to wake the zone.
    worker: tokio::task::JoinHandle<Node>,
}

/// A sleeping zone: an activator, a stand-in console that takes the wake request, and a worker that starts on the
/// activator's upstream address only once the console was asked (after `boot`, as a pod takes its time).
async fn sleeping_zone(
    worker_extra: &'static [(&'static str, &'static str)],
    boot: Duration,
) -> Zone {
    sleeping_zone_with(worker_extra, &[], boot).await
}

async fn sleeping_zone_with(
    worker_extra: &'static [(&'static str, &'static str)],
    activator_extra: &[(&str, &str)],
    boot: Duration,
) -> Zone {
    let (console_addr, console, woken) = recorder(202).await;
    // the upstream address: free now (refused, as a Service without endpoints), bound again on the wake
    let reserve = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let upstream = reserve.local_addr().unwrap();
    drop(reserve);
    let worker = tokio::spawn(async move {
        woken.notified().await;
        tokio::time::sleep(boot).await;
        let listener = loop {
            match TcpListener::bind(upstream).await {
                Ok(l) => break l,
                Err(_) => tokio::time::sleep(Duration::from_millis(20)).await,
            }
        };
        serve(config(worker_extra), listener).await
    });
    let (up, console_url) = (upstream.to_string(), format!("http://{console_addr}/"));
    let mut pairs = vec![
        ("RAMEN_ACTIVATOR", "1"),
        ("RAMEN_ACTIVATOR_UPSTREAM", up.as_str()),
        ("RAMEN_CONSOLE_INTERNAL_URL", console_url.as_str()),
        ("RAMEN_WAKE_TIMEOUT_SECS", "30"),
        // a python that does not exist: any attempt to start a runtime would fail loudly
        ("RAMEN_PYTHON", "/nonexistent/python"),
    ];
    pairs.extend_from_slice(activator_extra);
    let activator = serve(
        config(&pairs),
        TcpListener::bind("127.0.0.1:0").await.unwrap(),
    )
    .await;
    activator
        .app
        .activator
        .as_ref()
        .unwrap()
        .poll_ms
        .store(40, Relaxed);
    Zone {
        activator,
        console,
        worker,
    }
}

#[tokio::test]
async fn an_activator_is_healthy_reports_asleep_and_refuses_reload() {
    let z = sleeping_zone(&[], Duration::ZERO).await;
    let addr = z.activator.addr;
    let ch = tonic::transport::Endpoint::from_shared(format!("http://{addr}"))
        .unwrap()
        .connect()
        .await
        .unwrap();
    let mut hc = tonic_health::pb::health_client::HealthClient::new(ch.clone());
    for service in ["", "ramen.v1.Mcp", "ramen.v1.Admin"] {
        let st = hc
            .check(tonic_health::pb::HealthCheckRequest {
                service: service.into(),
            })
            .await
            .unwrap()
            .into_inner()
            .status;
        assert_eq!(
            st,
            tonic_health::pb::health_check_response::ServingStatus::Serving as i32,
            "{service:?}"
        );
    }
    assert_eq!(
        admin_metrics(addr).await,
        json!({"asleep": true, "last_call_at": 0, "woken": 0})
    );
    let mut admin = AdminClient::new(ch);
    let mut req = tonic::Request::new(ReloadRequest {});
    req.metadata_mut()
        .insert("x-ramen-admin-key", ADMIN_KEY.parse().unwrap());
    let st = admin.reload(req).await.unwrap_err();
    assert_eq!(st.code(), tonic::Code::FailedPrecondition);
    // Admin/* still wants the admin key
    let st = admin
        .metrics(tonic::Request::new(MetricsRequest {}))
        .await
        .unwrap_err();
    assert_eq!(st.code(), tonic::Code::Unauthenticated);
    assert!(
        !z.activator.app.sidecar.alive().await,
        "an activator never starts a runtime"
    );
    assert!(z.console.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_refused_call_never_wakes_the_zone() {
    let z = sleeping_zone(&[], Duration::ZERO).await;
    let addr = z.activator.addr;
    // no credential, a wrong one, a foreign browser origin, an invalid modern envelope, an unknown modern method
    assert_eq!(
        post(
            addr,
            &[],
            r#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#
        )
        .await
        .status,
        401
    );
    assert_eq!(
        legacy(addr, "rmk_wrong", "tools/list", json!({}))
            .await
            .status,
        401
    );
    let auth = format!("Bearer {MCP_KEY}");
    let r = post(
        addr,
        &[("Authorization", &auth), ("Origin", "https://evil.example")],
        r#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#,
    )
    .await;
    assert_eq!(r.status, 403);
    let r = post(
        addr,
        &[("Authorization", &auth), ("MCP-Protocol-Version", V)],
        r#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#,
    )
    .await;
    assert_eq!((r.status, r.code()), (400, Some(-32602)), "{}", r.body);
    let r = modern(addr, MCP_KEY, json!({}), "initialize", json!({}), &[]).await;
    assert_eq!((r.status, r.code()), (404, Some(-32601)), "{}", r.body);
    let st = grpc_call(
        addr,
        &[("authorization", "Bearer rmk_wrong")],
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
    )
    .await
    .unwrap_err();
    assert_eq!(st.code(), tonic::Code::Unauthenticated);
    tokio::time::sleep(Duration::from_millis(150)).await;
    assert!(
        z.console.lock().unwrap().is_empty(),
        "nothing was asked of the console"
    );
    assert_eq!(
        admin_metrics(addr).await,
        json!({"asleep": true, "last_call_at": 0, "woken": 0})
    );
    assert!(!z.worker.is_finished());
}

#[tokio::test]
async fn concurrent_calls_share_one_wake_and_are_answered_by_the_workers() {
    log_path();
    let z = sleeping_zone(&[], Duration::from_millis(300)).await;
    let addr = z.activator.addr;
    let auth = format!("Bearer {MCP_KEY}");
    let md = [("authorization", auth.as_str())];
    let (a, b, c, d) = tokio::join!(
        modern(
            addr,
            MCP_KEY,
            json!({}),
            "tools/call",
            json!({"name": "echo", "arguments": {"q": "through"}}),
            &[("X-Forwarded-For", "203.0.113.9")]
        ),
        legacy(addr, MCP_KEY, "tools/list", json!({})),
        grpc_call(
            addr,
            &md,
            json!({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "echo", "arguments": {"q": "g"}}})
        ),
        legacy(addr, "k2", "initialize", json!({})),
    );
    // the worker's own answers, relayed: envelope, status, headers
    assert_eq!(a.status, 200, "{}", a.body);
    assert_eq!(a.json()["result"]["resultType"], "complete", "{}", a.body);
    assert!(a.text().contains(r#""q": "through""#), "{}", a.body);
    assert_eq!(a.header("content-type"), Some("application/json"));
    assert!(
        b.json()["result"]["tools"]
            .as_array()
            .is_some_and(|t| t.len() > 3),
        "{}",
        b.body
    );
    assert_eq!(b.header("mcp-protocol-version"), Some("2025-06-18"));
    let c = c.expect("the gRPC call is relayed");
    assert_eq!(c["id"], 5);
    assert!(
        c["result"]["content"][0]["text"]
            .as_str()
            .unwrap()
            .contains(r#""q": "g""#)
    );
    // the session the worker minted comes back through the activator, and works on a later call
    let session = d
        .header("mcp-session-id")
        .expect("a session id")
        .to_string();
    let r = post(
        addr,
        &[("Authorization", "Bearer k2"), ("Mcp-Session-Id", &session)],
        r#"{"jsonrpc":"2.0","id":9,"method":"ping"}"#,
    )
    .await;
    assert_eq!(
        (r.status, &r.json()["result"]),
        (200, &json!({})),
        "{}",
        r.body
    );

    // exactly one wake request, as pinned
    let seen = z.console.lock().unwrap().clone();
    assert_eq!(seen.len(), 1, "one wake for four calls");
    let (path, headers, body) = &seen[0];
    assert_eq!(path, "/api/v1/internal/zones/demo/a/wake");
    assert_eq!(headers["x-ramen-admin-key"], ADMIN_KEY);
    assert_eq!(headers["content-type"], "application/json");
    assert_eq!(
        serde_json::from_str::<Value>(body).unwrap(),
        json!({"trigger": "call"})
    );
    let m = admin_metrics(addr).await;
    assert_eq!(
        (&m["asleep"], &m["woken"]),
        (&json!(true), &json!(1)),
        "{m}"
    );
    assert!(m["last_call_at"].as_u64().unwrap() > 0);
    assert!(!z.activator.app.sidecar.alive().await);

    // the workers are up: the activator keeps forwarding, without another wake
    let r = legacy(addr, MCP_KEY, "tools/list", json!({})).await;
    assert_eq!(r.status, 200);
    assert_eq!(z.console.lock().unwrap().len(), 1);
    // the worker saw the caller's headers as they came: its own log line names the forwarded address only when
    // it trusts a proxy (it does not here), and the credential's key id — the same one the activator logged
    let w = z.worker.await.unwrap();
    assert!(w.app.metrics.last_call_at.load(Relaxed) > 0);
    let line = log_line(|v| v["activator"] == true && v["forwarded"] == true && v["woke"] == true)
        .expect("the activator's access line");
    assert_eq!(
        (&line["status"], &line["zone"], &line["grpc_code"]),
        (&json!("ok"), &json!("a"), &json!("OK")),
        "{line}"
    );
    assert!(line["key_id"].is_string() && line["method"].is_string());
    // and the worker's refusals are relayed as they are
    let r = legacy(addr, MCP_KEY, "tools/call", json!({"name": "nope"})).await;
    assert_eq!((r.status, r.code()), (200, Some(-32004)), "{}", r.body);
}

/// SSE answers and `subscriptions/listen` pass through as they are produced.
#[tokio::test]
async fn streams_pass_through_the_activator_unbuffered() {
    let z = sleeping_zone(&[], Duration::ZERO).await;
    let addr = z.activator.addr;
    let sse = ("Accept", "application/json, text/event-stream");
    let r = modern(
        addr,
        MCP_KEY,
        json!({}),
        "tools/call",
        json!({"name": "prog", "arguments": {}, "_meta": {"progressToken": "p1"}}),
        &[sse],
    )
    .await;
    assert_eq!(r.status, 200, "{}", r.body);
    assert_eq!(r.header("content-type"), Some("text/event-stream"));
    assert_eq!(r.header("x-accel-buffering"), Some("no"));
    let events = r.events();
    assert_eq!(
        events.len(),
        4,
        "three progress lines and the result: {}",
        r.body
    );
    assert_eq!(events[1]["params"]["message"], "half");
    assert!(events[3]["result"]["content"][0]["text"].is_string());

    // a subscription: the ack arrives while the stream stays open
    let params = json!({"notifications": {"toolsListChanged": true}, "_meta": meta(json!({}))});
    let body =
        json!({"jsonrpc": "2.0", "id": "sub-1", "method": "subscriptions/listen", "params": params})
            .to_string();
    let auth = format!("Bearer {MCP_KEY}");
    let hs = [
        ("Authorization", auth.as_str()),
        ("MCP-Protocol-Version", V),
        ("Mcp-Method", "subscriptions/listen"),
        sse,
    ];
    let mut s = TcpStream::connect(addr).await.unwrap();
    s.write_all(request(addr, &hs, &body).as_bytes())
        .await
        .unwrap();
    let mut got = String::new();
    let mut buf = [0u8; 4096];
    let ack = tokio::time::timeout(Duration::from_secs(10), async {
        while !got.contains("notifications/subscriptions/acknowledged") {
            let n = s.read(&mut buf).await.unwrap();
            assert!(n > 0, "the stream ended before the ack: {got}");
            got.push_str(&String::from_utf8_lossy(&buf[..n]));
        }
    })
    .await;
    assert!(ack.is_ok(), "the ack was held back: {got}");
    assert!(got.starts_with("HTTP/1.1 200"), "{got}");
    let w = z.worker.await.unwrap();
    assert_eq!(w.app.subs.open(), 1, "the worker holds the stream");
    // a reload on the worker reaches the client through the activator while the stream is open
    std::fs::write(w.app.cfg.read().await.bucket.join("hash"), "h2").unwrap();
    w.app.load().await.unwrap();
    let pushed = tokio::time::timeout(Duration::from_secs(10), async {
        while !got.contains("notifications/tools/list_changed") {
            let n = s.read(&mut buf).await.unwrap();
            assert!(n > 0, "the stream ended early: {got}");
            got.push_str(&String::from_utf8_lossy(&buf[..n]));
        }
    })
    .await;
    assert!(pushed.is_ok(), "{got}");
    // the client hangs up: the worker's stream closes too
    drop(s);
    let closed = tokio::time::timeout(Duration::from_secs(10), async {
        while w.app.subs.open() != 0 {
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    })
    .await;
    assert!(closed.is_ok(), "the upstream stream was left open");
}

#[tokio::test]
async fn a_zone_that_does_not_wake_is_503_with_retry_after_and_the_wake_is_retried() {
    log_path();
    // a console that refuses the wake, and nobody on the upstream address
    let (console_addr, console, _) = recorder(500).await;
    let a = serve(
        config(&[
            ("RAMEN_ACTIVATOR", "1"),
            ("RAMEN_ACTIVATOR_UPSTREAM", "127.0.0.1:1"),
            (
                "RAMEN_CONSOLE_INTERNAL_URL",
                &format!("http://{console_addr}"),
            ),
            ("RAMEN_WAKE_TIMEOUT_SECS", "1"),
            ("RAMEN_ZONE", "stuck"),
        ]),
        TcpListener::bind("127.0.0.1:0").await.unwrap(),
    )
    .await;
    a.app.activator.as_ref().unwrap().poll_ms.store(40, Relaxed);
    let r = legacy(a.addr, MCP_KEY, "tools/list", json!({})).await;
    assert_eq!(r.status, 503, "{}", r.body);
    assert_eq!(r.header("retry-after"), Some("10"));
    let auth = format!("Bearer {MCP_KEY}");
    let st = grpc_call(
        a.addr,
        &[("authorization", &auth)],
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
    )
    .await
    .unwrap_err();
    assert_eq!(st.code(), tonic::Code::Unavailable);
    // the refused wake request was made again by the next call; none of them counts as a wake
    let seen = console.lock().unwrap().clone();
    assert_eq!(seen.len(), 2, "one wake request per call after a failure");
    assert!(
        seen.iter()
            .all(|(p, _, _)| p == "/api/v1/internal/zones/demo/stuck/wake")
    );
    assert_eq!(admin_metrics(a.addr).await["woken"], 0);
    for transport in ["http", "grpc"] {
        let line = log_line(|v| {
            v["zone"] == "stuck" && v["reason"] == "waking" && v["transport"] == transport
        })
        .unwrap_or_else(|| panic!("no waking line for {transport}"));
        assert_eq!(
            (&line["status"], &line["grpc_code"], &line["activator"]),
            (&json!("denied"), &json!("UNAVAILABLE"), &json!(true)),
            "{line}"
        );
    }
}

/// While workers answer the activator forwards — and still asks for the wake, once: right after a sleep those are
/// the old workers draining, and a call they served must not leave the zone asleep. Gone, the next call asks again.
#[tokio::test]
async fn it_forwards_while_the_workers_are_up_and_wakes_them_again_when_they_left() {
    let (console_addr, console, _) = recorder(200).await;
    let w1 = worker(&[]).await;
    let upstream = w1.addr;
    let a = serve(
        config(&[
            ("RAMEN_ACTIVATOR", "1"),
            ("RAMEN_ACTIVATOR_UPSTREAM", &format!("http://{upstream}")),
            (
                "RAMEN_CONSOLE_INTERNAL_URL",
                &format!("http://{console_addr}"),
            ),
            ("RAMEN_WAKE_TIMEOUT_SECS", "2"),
        ]),
        TcpListener::bind("127.0.0.1:0").await.unwrap(),
    )
    .await;
    a.app.activator.as_ref().unwrap().poll_ms.store(40, Relaxed);
    let auth = format!("Bearer {MCP_KEY}");
    let md = [("authorization", auth.as_str())];
    let list = json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"});
    // the sleep sequence applies the activator before the workers go: it forwards, and asks for the wake once
    assert_eq!(
        legacy(a.addr, MCP_KEY, "tools/list", json!({}))
            .await
            .status,
        200
    );
    assert!(grpc_call(a.addr, &md, list.clone()).await.unwrap()["result"]["tools"].is_array());
    for _ in 0..50 {
        if admin_metrics(a.addr).await["woken"] == 1 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert_eq!(
        console.lock().unwrap().len(),
        1,
        "two calls, one wake request"
    );
    assert_eq!(admin_metrics(a.addr).await["woken"], 1);
    // the workers are scaled to 0
    let Node {
        app: w1_app, _stop, ..
    } = w1;
    _stop.send(()).unwrap();
    w1_app.sidecar.kill("test").await;
    loop {
        if let Ok(free) = TcpListener::bind(upstream).await {
            drop(free);
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    tokio::time::sleep(Duration::from_millis(1100)).await; // past the freshness of the last SERVING answer
    // the wake was already asked for; nobody coming up, the call is told to retry
    let r = legacy(a.addr, MCP_KEY, "tools/list", json!({})).await;
    assert_eq!(
        (r.status, r.header("retry-after")),
        (503, Some("10")),
        "{}",
        r.body
    );
    assert_eq!(console.lock().unwrap().len(), 1);
    let st = grpc_call(a.addr, &md, list.clone()).await.unwrap_err();
    assert_eq!(st.code(), tonic::Code::Unavailable);
    // the console had taken the request and the zone never came: the next call asked again
    for _ in 0..50 {
        if admin_metrics(a.addr).await["woken"] == 2 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert_eq!(console.lock().unwrap().len(), 2);
    assert_eq!(admin_metrics(a.addr).await["woken"], 2);
    // the workers come back on the same address: answered again
    let _w2 = serve(config(&[]), TcpListener::bind(upstream).await.unwrap()).await;
    let r = legacy(a.addr, MCP_KEY, "tools/list", json!({})).await;
    assert_eq!(r.status, 200, "{}", r.body);
    assert!(grpc_call(a.addr, &md, list).await.unwrap()["result"]["tools"].is_array());
}

/// An activator pod can outlive a wake (scaled to 0 and straight back to 1 when the zone is put to sleep again):
/// it must not go on believing the wake it asked for earlier, even while draining workers still answer.
#[tokio::test]
async fn an_activator_that_outlives_a_wake_asks_again() {
    let (console_addr, console, _) = recorder(200).await;
    let w = worker(&[]).await;
    let a = serve(
        config(&[
            ("RAMEN_ACTIVATOR", "1"),
            ("RAMEN_ACTIVATOR_UPSTREAM", &format!("http://{}", w.addr)),
            (
                "RAMEN_CONSOLE_INTERNAL_URL",
                &format!("http://{console_addr}"),
            ),
        ]),
        TcpListener::bind("127.0.0.1:0").await.unwrap(),
    )
    .await;
    let call = || legacy(a.addr, MCP_KEY, "tools/list", json!({}));
    assert_eq!(call().await.status, 200);
    assert_eq!(call().await.status, 200);
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(console.lock().unwrap().len(), 1, "one wake for a burst");
    tokio::time::sleep(Duration::from_millis(5200)).await;
    assert_eq!(call().await.status, 200);
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(console.lock().unwrap().len(), 2, "a later call asks again");
}

/// A forwarded call is judged once, as the original caller: the worker takes the client address from the
/// activator (proved with the zone's session secret) for its CIDR check and its log, and from nobody else.
#[tokio::test]
async fn the_worker_judges_a_forwarded_call_as_the_original_caller() {
    log_path();
    // the zone admits one client range; the activator learns the client from its edge proxy, the worker (reached
    // from the activator's own address) would not
    let z = sleeping_zone_with(
        &[
            ("RAMEN_ALLOWED_CIDRS", "203.0.113.0/24"),
            ("RAMEN_ZONE", "ipz"),
        ],
        &[
            ("RAMEN_ALLOWED_CIDRS", "203.0.113.0/24"),
            ("RAMEN_TRUST_PROXY_HOPS", "1"),
            ("RAMEN_ZONE", "ipz"),
        ],
        Duration::ZERO,
    )
    .await;
    let addr = z.activator.addr;
    let auth = format!("Bearer {MCP_KEY}");
    // what a client sends in these two headers is thrown away by the activator
    let r = post(
        addr,
        &[
            ("Authorization", &auth),
            ("X-Forwarded-For", "203.0.113.9"),
            ("x-ramen-client-ip", "203.0.113.77"),
            ("x-ramen-activator", "00ff"),
        ],
        r#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#,
    )
    .await;
    assert_eq!(r.status, 200, "{}", r.body);
    let md = [
        ("authorization", auth.as_str()),
        ("x-forwarded-for", "203.0.113.10"),
    ];
    let list = json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"});
    assert!(grpc_call(addr, &md, list.clone()).await.unwrap()["result"]["tools"].is_array());
    for (transport, ip) in [("http", "203.0.113.9"), ("grpc", "203.0.113.10")] {
        let line = log_line(|v| {
            v["zone"] == "ipz" && v["via"] == "activator" && v["transport"] == transport
        })
        .unwrap_or_else(|| panic!("no worker line for {transport}"));
        assert_eq!(
            (&line["ip"], &line["status"]),
            (&json!(ip), &json!("ok")),
            "{line}"
        );
        assert!(line.get("activator").is_none(), "the worker's line: {line}");
    }
    let all = std::fs::read_to_string(log_path()).unwrap();
    assert!(!all.contains("x-ramen-activator") && !all.contains("x-ramen-client-ip"));
    // a caller outside the range is refused by the activator itself, and wakes nobody further
    let r = post(
        addr,
        &[
            ("Authorization", &auth),
            ("X-Forwarded-For", "198.51.100.1"),
        ],
        r#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#,
    )
    .await;
    assert_eq!(r.status, 403);
    // straight at the worker: the headers without the proof — or with a wrong one — are not there at all
    let w = z.worker.await.unwrap();
    for extra in [
        vec![("x-ramen-client-ip", "203.0.113.9")],
        vec![
            ("x-ramen-client-ip", "203.0.113.9"),
            (
                "x-ramen-activator",
                "0000000000000000000000000000000000000000000000000000000000000000",
            ),
        ],
    ] {
        let mut hs = vec![("Authorization", auth.as_str())];
        hs.extend(extra);
        let r = post(
            w.addr,
            &hs,
            r#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#,
        )
        .await;
        assert_eq!(r.status, 403, "{}", r.body);
    }
    let mut md = md.to_vec();
    md.push(("x-ramen-client-ip", "203.0.113.9"));
    md.push(("x-ramen-activator", "nope"));
    let st = grpc_call(w.addr, &md, list).await.unwrap_err();
    assert_eq!(st.code(), tonic::Code::PermissionDenied);
    // the proof of another zone (same secret) is not this zone's
    let other = ramen_node::grpc::activator_proof(
        &ramen_node::session::Sessions::new(Some("s"), 60),
        "demo",
        "elsewhere",
    );
    let r = post(
        w.addr,
        &[
            ("Authorization", &auth),
            ("x-ramen-client-ip", "203.0.113.9"),
            ("x-ramen-activator", &other),
        ],
        r#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#,
    )
    .await;
    assert_eq!(r.status, 403);
}

/// The console's own calls (worker admin key next to the credential) are answered but do not reset the idle clock.
#[tokio::test]
async fn the_consoles_own_calls_do_not_keep_a_zone_awake() {
    let n = worker(&[]).await;
    let auth = format!("Bearer {MCP_KEY}");
    let list = json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"});
    let r = post(
        n.addr,
        &[("Authorization", &auth), ("x-ramen-admin-key", ADMIN_KEY)],
        &list.to_string(),
    )
    .await;
    assert_eq!(r.status, 200, "{}", r.body);
    let md = [
        ("authorization", auth.as_str()),
        ("x-ramen-admin-key", ADMIN_KEY),
    ];
    let call = json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {}}});
    assert!(grpc_call(n.addr, &md, call.clone()).await.unwrap()["result"]["content"].is_array());
    assert_eq!(admin_metrics(n.addr).await["last_call_at"], 0);
    // a wrong admin key is just a caller's header: the call counts
    let md = [
        ("authorization", auth.as_str()),
        ("x-ramen-admin-key", "adm-wrong"),
    ];
    grpc_call(n.addr, &md, call.clone()).await.unwrap();
    assert!(
        admin_metrics(n.addr).await["last_call_at"]
            .as_u64()
            .unwrap()
            > 0
    );
    // through an activator: answered (the zone wakes), and neither idle clock moves
    let z = sleeping_zone(&[], Duration::ZERO).await;
    let md = [
        ("authorization", auth.as_str()),
        ("x-ramen-admin-key", ADMIN_KEY),
    ];
    assert!(grpc_call(z.activator.addr, &md, call).await.unwrap()["result"]["content"].is_array());
    assert_eq!(admin_metrics(z.activator.addr).await["last_call_at"], 0);
    let w = z.worker.await.unwrap();
    assert_eq!(w.app.metrics.last_call_at.load(Relaxed), 0);
}

// ---------------------------------------------------------------------------------------------- §24.3 consent

const CONSENT: &[(&str, &str)] = &[
    ("RAMEN_TELEMETRY_CONSENT", "1"),
    ("RAMEN_TELEMETRY_NOTICE", "jaeger.test:4318"),
    ("RAMEN_OAUTH_ISSUER", "https://console.example"),
];

/// The §16.2 binding of an `rmk_` key: the SHA-256 of the key, hex.
fn binding_of(key: &str) -> String {
    ring::digest::digest(&ring::digest::SHA256, key.as_bytes())
        .as_ref()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn elicit() -> Value {
    json!({"elicitation": {}})
}

fn accept(yes: bool) -> Value {
    json!({ KEY: {"action": "accept", "content": {"accept": yes}} })
}

fn with(mut params: Value, extra: Value) -> Value {
    params
        .as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    params
}

#[tokio::test]
async fn consent_by_prompt_then_the_tools_own_rounds() {
    log_path();
    let n = worker(CONSENT).await;
    // §26.3: once consent is on record the call must also report (an empty report here)
    let ask = || {
        with(
            json!({"name": "ask", "arguments": {"mode": "state"}}),
            empty_report(),
        )
    };
    let call = |params: Value| modern(n.addr, MCP_KEY, elicit(), "tools/call", params, &[]);

    // round 1: the node asks, the runtime is not reached
    let r = call(ask()).await;
    assert_eq!(r.status, 200, "{}", r.body);
    let first = r.json()["result"].clone();
    assert_eq!(first["resultType"], "input_required", "{}", r.body);
    let requests = first["inputRequests"].as_object().unwrap();
    assert_eq!(requests.keys().collect::<Vec<_>>(), vec![KEY]);
    let prompt = &requests[KEY];
    assert_eq!(prompt["method"], "elicitation/create");
    assert_eq!(prompt["params"]["mode"], "form");
    assert_eq!(
        prompt["params"]["message"],
        "This Ramen group records telemetry about your calls (tool name, timing, outcome — never arguments or \
         results) at jaeger.test:4318 and asks your agent to send its telemetry there. Accept to continue."
    );
    assert_eq!(
        prompt["params"]["requestedSchema"]["properties"]["accept"]["type"],
        "boolean"
    );
    let consent_state = first["requestState"].as_str().expect("signed").to_string();
    let line = log_line(|v| v["reason"] == "consent_required" && v["name"] == "ask").unwrap();
    assert_eq!(line["status"], "denied", "{line}");

    // declining — `accept: false`, `decline`, `cancel` — is -32602 and records nothing
    for answer in [
        accept(false),
        json!({ KEY: {"action": "decline"} }),
        json!({ KEY: {"action": "cancel"} }),
    ] {
        let r = call(with(
            ask(),
            json!({"inputResponses": answer, "requestState": consent_state}),
        ))
        .await;
        assert_eq!((r.status, r.code()), (200, Some(-32602)), "{}", r.body);
        assert_eq!(r.json()["error"]["message"], "telemetry consent declined");
    }
    assert!(log_line(|v| v["reason"] == "consent_declined").is_some());
    // an acceptance needs the state this node signed for this caller and this request
    for bad in [
        json!({"inputResponses": accept(true)}),
        json!({"inputResponses": accept(true), "requestState": "x.y"}),
    ] {
        let r = call(with(ask(), bad)).await;
        assert_eq!(r.code(), Some(-32602), "{}", r.body);
        assert_eq!(
            r.json()["error"]["message"],
            "requestState integrity check failed"
        );
    }
    let other_args = with(
        json!({"name": "ask", "arguments": {"mode": "plain"}}),
        json!({"inputResponses": accept(true), "requestState": consent_state}),
    );
    assert_eq!(call(other_args).await.code(), Some(-32602));
    let as_k2 = modern(
        n.addr,
        "k2",
        elicit(),
        "tools/call",
        with(
            ask(),
            json!({"inputResponses": accept(true), "requestState": consent_state}),
        ),
        &[],
    )
    .await;
    assert_eq!(as_k2.code(), Some(-32602), "another caller's state");
    // …and the consent round's state is not a tool state: without the answer it reaches the tool check and fails
    // (still nothing recorded, so the caller is simply asked again)
    let r = call(with(ask(), json!({"requestState": consent_state}))).await;
    assert_eq!(r.json()["result"]["resultType"], "input_required");

    // round 2: accepted — recorded, and the call runs: the tool's own input request follows, with ITS state
    let r = call(with(
        ask(),
        json!({"inputResponses": accept(true), "requestState": consent_state}),
    ))
    .await;
    let second = r.json()["result"].clone();
    assert_eq!(second["resultType"], "input_required", "{}", r.body);
    assert_eq!(
        second["inputRequests"]
            .as_object()
            .unwrap()
            .keys()
            .collect::<Vec<_>>(),
        vec!["user_name"]
    );
    let tool_state = second["requestState"]
        .as_str()
        .expect("the tool's state")
        .to_string();
    assert_ne!(tool_state, consent_state);
    let line = log_line(|v| v["consent"] == "granted").expect("the grant is logged");
    assert_eq!(
        (&line["status"], &line["name"]),
        (&json!("ok"), &json!("ask"))
    );

    // round 3: the tool's answer with the tool's state — a client that still sends the consent answer is fine
    let mut answers = accept(true);
    answers["user_name"] = json!({"action": "accept", "content": {"name": "Ada"}});
    let r = call(with(
        ask(),
        json!({"inputResponses": answers, "requestState": tool_state}),
    ))
    .await;
    assert_eq!(r.text(), r#"Hello, Ada! state={"round": 1}"#, "{}", r.body);
    assert_eq!(r.json()["result"]["resultType"], "complete");

    // on record: never asked again — a stale consent state on a later call is dropped, not handed to the tool
    let r = call(with(
        with(
            json!({"name": "echo", "arguments": {"q": "1"}}),
            empty_report(),
        ),
        json!({"inputResponses": accept(true)}),
    ))
    .await;
    let seen: Value = serde_json::from_str(&r.text()).unwrap();
    assert_eq!(
        (&seen["inputResponses"], &seen["state"]),
        (&Value::Null, &Value::Null),
        "the tool never sees the consent round: {seen}"
    );
    let r = call(with(ask(), json!({"requestState": consent_state}))).await;
    assert_eq!(
        r.json()["result"]["inputRequests"]["user_name"]["method"],
        "elicitation/create",
        "{}",
        r.body
    );
    // …on the legacy era too (with the report in the body, §26.3), and over gRPC; another caller is still asked
    let r = legacy(
        n.addr,
        MCP_KEY,
        "tools/call",
        with(json!({"name": "echo", "arguments": {}}), empty_report()),
    )
    .await;
    assert!(r.json()["result"]["content"].is_array(), "{}", r.body);
    let r = modern(
        n.addr,
        "k2",
        elicit(),
        "tools/call",
        json!({"name": "echo", "arguments": {}}),
        &[],
    )
    .await;
    assert!(r.json()["result"]["inputRequests"][KEY].is_object());
    // the record: group + caller binding, 30 days
    assert_eq!(ramen_node::consent::TTL_SECS, 30 * 24 * 3600);
    let binding = binding_of(MCP_KEY);
    let key = format!("ramen:consent:demo:{binding}");
    assert_eq!(
        ramen_node::consent::store_key(&*n.app.cfg.read().await, &binding),
        key
    );
    assert!(n.app.consent.get(&key).await.unwrap().is_some());
}

#[tokio::test]
async fn consent_refusals_by_era_and_what_is_never_gated() {
    let n = worker(CONSENT).await;
    // legacy: -32003 with the pinned text
    let r = legacy(
        n.addr,
        MCP_KEY,
        "tools/call",
        json!({"name": "echo", "arguments": {}}),
    )
    .await;
    assert_eq!((r.status, r.code()), (200, Some(-32003)), "{}", r.body);
    assert_eq!(
        r.json()["error"]["message"],
        "forbidden: this group requires telemetry consent; use a client that speaks MCP 2026-07-28 or sign in through OAuth"
    );
    // modern without elicitation: -32021 naming the capability, HTTP 400 — for each gated method
    for (method, params) in [
        ("tools/call", json!({"name": "echo", "arguments": {}})),
        (
            "prompts/get",
            json!({"name": "greet", "arguments": {"tone": "warm", "who": "x"}}),
        ),
        ("resources/read", json!({"uri": "ramen://demo/readme"})),
    ] {
        let r = modern(n.addr, MCP_KEY, json!({}), method, params.clone(), &[]).await;
        assert_eq!(
            (r.status, r.code()),
            (400, Some(-32021)),
            "{method}: {}",
            r.body
        );
        assert_eq!(
            r.json()["error"]["data"]["requiredCapabilities"],
            json!({"elicitation": {}})
        );
        // …and with it, each is asked
        let r = modern(n.addr, MCP_KEY, elicit(), method, params, &[]).await;
        assert!(
            r.json()["result"]["inputRequests"][KEY].is_object(),
            "{method}: {}",
            r.body
        );
        assert!(r.json()["result"]["requestState"].is_string());
    }
    // over gRPC the same gate (the body selects the era)
    let auth = format!("Bearer {MCP_KEY}");
    let md = [("authorization", auth.as_str())];
    let g = grpc_call(
        n.addr,
        &md,
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {}}}),
    )
    .await
    .unwrap();
    assert_eq!(g["error"]["code"], -32003, "{g}");
    // lists, discover and completion are not gated
    for method in [
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
        "server/discover",
    ] {
        let r = modern(n.addr, MCP_KEY, json!({}), method, json!({}), &[]).await;
        assert_eq!(r.status, 200, "{method}: {}", r.body);
        assert_eq!(r.json()["result"]["resultType"], "complete", "{method}");
    }
    let r = modern(
        n.addr,
        MCP_KEY,
        json!({}),
        "completion/complete",
        json!({"ref": {"type": "ref/prompt", "name": "greet"}, "argument": {"name": "tone", "value": "w"}}),
        &[],
    )
    .await;
    assert_eq!(r.json()["result"]["completion"]["total"], 2, "{}", r.body);
    assert_eq!(
        legacy(n.addr, MCP_KEY, "tools/list", json!({}))
            .await
            .status,
        200
    );
    // an unknown tool is answered as ever (the access checks come first for hidden/forbidden ones; an unknown
    // name is asked about consent like any call and then not found)
    // a group without the setting is not gated at all
    let open = worker(&[]).await;
    let r = legacy(
        open.addr,
        MCP_KEY,
        "tools/call",
        json!({"name": "echo", "arguments": {}}),
    )
    .await;
    assert!(r.json()["result"]["content"].is_array(), "{}", r.body);
}

#[tokio::test]
async fn hidden_and_forbidden_tools_are_answered_before_anyone_is_asked() {
    let n = worker(&[
        ("RAMEN_TELEMETRY_CONSENT", "1"),
        ("RAMEN_BLOCKED", "secret_tool"),
        (
            "RAMEN_TOOL_ACCESS",
            r#"{"ask": {"list": [], "call": []}, "prog": {"list": ["key"], "call": []}}"#,
        ),
    ])
    .await;
    for name in ["secret_tool", "ask"] {
        let r = modern(
            n.addr,
            MCP_KEY,
            elicit(),
            "tools/call",
            json!({"name": name, "arguments": {}}),
            &[],
        )
        .await;
        assert_eq!(r.code(), Some(-32602), "{name}: {}", r.body);
        assert_eq!(
            r.json()["error"]["message"],
            format!("Unknown tool: {name}")
        );
    }
    let r = modern(
        n.addr,
        MCP_KEY,
        elicit(),
        "tools/call",
        json!({"name": "prog", "arguments": {}}),
        &[],
    )
    .await;
    assert!(
        r.json()["error"]["message"]
            .as_str()
            .unwrap()
            .starts_with("forbidden: prog"),
        "{}",
        r.body
    );
}

#[tokio::test]
async fn a_tel_token_and_the_consoles_own_calls_are_never_asked() {
    let n = worker(CONSENT).await;
    let key = ramen_node::session::Sessions::new(Some("s"), 60).derived_key("oauth");
    let token = |sub: &str, tel: Value| {
        let mut c = json!({"iss": "https://console.example", "sub": sub, "aud": "mcp:demo:a", "scope": "mcp:demo:a",
                           "exp": ramen_node::session::now() + 600});
        if !tel.is_null() {
            c["tel"] = tel;
        }
        ramen_node::token::mint(&c, &key)
    };
    let echo = || json!({"name": "echo", "arguments": {"q": "t"}});
    // 1. the person approved the notice on the OAuth consent page: never asked, nothing recorded — but §26.3
    // still wants the report, which a 2025-06-18 client cannot give
    let t = token("u-yes", json!(true));
    let r = legacy(n.addr, &t, "tools/call", echo()).await;
    assert_eq!(r.code(), Some(-32003), "{}", r.body);
    assert!(
        r.json()["error"]["message"]
            .as_str()
            .unwrap()
            .starts_with("forbidden: this group requires telemetry;"),
        "{}",
        r.body
    );
    let r = modern(
        n.addr,
        &t,
        json!({}),
        "tools/call",
        with(echo(), empty_report()),
        &[],
    )
    .await;
    assert_eq!(r.json()["result"]["resultType"], "complete", "{}", r.body);
    assert!(
        n.app
            .consent
            .get("ramen:consent:demo:user:u-yes")
            .await
            .unwrap()
            .is_none()
    );
    // a token without the claim — or with anything but `true` — is asked like a key
    for tel in [Value::Null, json!(false), json!("true"), json!(1)] {
        let t = token("u-no", tel.clone());
        let r = legacy(n.addr, &t, "tools/call", echo()).await;
        assert_eq!(r.code(), Some(-32003), "tel={tel}: {}", r.body);
    }
    // …and its prompt-given consent is recorded for the person, not the token
    let t = token("u-no", Value::Null);
    let r = modern(n.addr, &t, elicit(), "tools/call", echo(), &[]).await;
    let state = r.json()["result"]["requestState"]
        .as_str()
        .unwrap()
        .to_string();
    let r = modern(
        n.addr,
        &t,
        elicit(),
        "tools/call",
        with(
            with(echo(), empty_report()),
            json!({"inputResponses": accept(true), "requestState": state}),
        ),
        &[],
    )
    .await;
    assert_eq!(r.json()["result"]["resultType"], "complete", "{}", r.body);
    let later = token("u-no", Value::Null); // a refreshed token: consent on record, so only the report is missing
    let r = legacy(n.addr, &later, "tools/call", echo()).await;
    assert!(
        r.json()["error"]["message"]
            .as_str()
            .unwrap()
            .starts_with("forbidden: this group requires telemetry;"),
        "{}",
        r.body
    );
    assert!(
        n.app
            .consent
            .get("ramen:consent:demo:user:u-no")
            .await
            .unwrap()
            .is_some()
    );

    // 2. the console's own calls: the group's MCP key plus the worker admin key in `x-ramen-admin-key`
    let r = modern(
        n.addr,
        "k2",
        json!({}),
        "tools/call",
        echo(),
        &[("x-ramen-admin-key", ADMIN_KEY)],
    )
    .await;
    assert_eq!(r.json()["result"]["resultType"], "complete", "{}", r.body);
    let md = [
        ("authorization", "Bearer k2"),
        ("x-ramen-admin-key", ADMIN_KEY),
    ];
    let body = json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": echo()});
    let g = grpc_call(n.addr, &md, body.clone()).await.unwrap();
    assert!(g["result"]["content"].is_array(), "{g}");
    let k2 = format!("ramen:consent:demo:{}", binding_of("k2"));
    assert!(n.app.consent.get(&k2).await.unwrap().is_none());
    // a wrong admin key is no bypass, and the admin key alone is no credential
    let md = [
        ("authorization", "Bearer k2"),
        ("x-ramen-admin-key", "adm-wrong"),
    ];
    let g = grpc_call(n.addr, &md, body.clone()).await.unwrap();
    assert_eq!(g["error"]["code"], -32003, "{g}");
    let st = grpc_call(n.addr, &[("x-ramen-admin-key", ADMIN_KEY)], body)
        .await
        .unwrap_err();
    assert_eq!(st.code(), tonic::Code::Unauthenticated);
}

/// The activator does not apply the gate; the worker does, to the forwarded call — with the caller's headers.
#[tokio::test]
async fn the_gate_is_the_workers_also_behind_an_activator() {
    let z = sleeping_zone(&[("RAMEN_TELEMETRY_CONSENT", "1")], Duration::ZERO).await;
    let addr = z.activator.addr;
    let echo = json!({"name": "echo", "arguments": {}});
    let r = legacy(addr, MCP_KEY, "tools/call", echo.clone()).await;
    assert_eq!(
        r.code(),
        Some(-32003),
        "the worker's refusal, relayed: {}",
        r.body
    );
    assert_eq!(z.console.lock().unwrap().len(), 1, "it did wake the zone");
    // the console's own call through the activator: `x-ramen-admin-key` travels with the request
    let r = modern(
        addr,
        MCP_KEY,
        json!({}),
        "tools/call",
        echo,
        &[("x-ramen-admin-key", ADMIN_KEY)],
    )
    .await;
    assert_eq!(r.json()["result"]["resultType"], "complete", "{}", r.body);
}

#[tokio::test]
async fn discover_tells_a_client_where_to_send_its_telemetry() {
    let client = r#"{"otlp_endpoint": "http://jaeger.test:4318", "headers": {"x-team": "demo"}, "resource_attributes": {"ramen.group": "demo", "ramen.zone": "a"}}"#;
    let n = worker(&[("RAMEN_OTEL_CLIENT", client)]).await;
    let r = modern(
        n.addr,
        MCP_KEY,
        json!({}),
        "server/discover",
        json!({}),
        &[],
    )
    .await;
    let meta = &r.json()["result"]["_meta"];
    assert_eq!(
        meta["io.ramen/telemetry"],
        serde_json::from_str::<Value>(client).unwrap(),
        "{}",
        r.body
    );
    assert_eq!(
        meta["io.modelcontextprotocol/serverInfo"]["name"],
        "ramen-node"
    );
    // only there, and only when the deploy handed it over
    let r = modern(n.addr, MCP_KEY, json!({}), "tools/list", json!({}), &[]).await;
    assert!(
        r.json()["result"]["_meta"]
            .get("io.ramen/telemetry")
            .is_none()
    );
    let plain = worker(&[]).await;
    let r = modern(
        plain.addr,
        MCP_KEY,
        json!({}),
        "server/discover",
        json!({}),
        &[],
    )
    .await;
    assert!(
        r.json()["result"]["_meta"]
            .get("io.ramen/telemetry")
            .is_none()
    );
    assert!(r.json()["result"]["supportedVersions"].is_array());
}

// ------------------------------------------------------------------------------------------------ §24.3 export

fn attr<'a>(item: &'a Value, key: &str) -> Option<&'a str> {
    item["attributes"]
        .as_array()?
        .iter()
        .find(|a| a["key"] == key)?["value"]["stringValue"]
        .as_str()
}

#[tokio::test]
async fn calls_are_exported_as_spans_and_counts_without_arguments_results_or_credentials() {
    let (collector, seen, _) = recorder(200).await;
    let n = worker(&[
        ("RAMEN_OTEL_ENDPOINT", &format!("http://{collector}")),
        ("RAMEN_OTEL_HEADERS", r#"{"x-otlp-key": "col-s3cr3t-key"}"#),
        ("RAMEN_ENV", "prod"),
        ("RAMEN_VERBOSE", "1"), // the access log may quote the request; the export never does
        ("RAMEN_BLOCKED", "secret_tool"),
    ])
    .await;
    const ARG: &str = "arg-value-4f2c9d1e-do-not-export";
    let parent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01";
    let r = modern(
        n.addr,
        MCP_KEY,
        json!({}),
        "tools/call",
        json!({"name": "echo", "arguments": {"q": ARG}}),
        &[("traceparent", parent)],
    )
    .await;
    assert!(
        r.text().contains(ARG),
        "the tool echoes its argument: {}",
        r.body
    );
    legacy(n.addr, MCP_KEY, "tools/list", json!({})).await;
    legacy(
        n.addr,
        MCP_KEY,
        "tools/call",
        json!({"name": "secret_tool", "arguments": {"q": ARG}}),
    )
    .await;
    legacy(
        n.addr,
        MCP_KEY,
        "resources/read",
        json!({"uri": "ramen://demo/readme"}),
    )
    .await;
    let auth = format!("Bearer {MCP_KEY}");
    grpc_call(
        n.addr,
        &[
            ("authorization", &auth),
            ("x-ramen-admin-key", ADMIN_KEY),
            // a bridge client's trace continues over gRPC metadata too
            ("traceparent", "00-1bf7651916cd43dd8448eb211c80319c-c7ad6b7169203331-01"),
            ("tracestate", "vendor=1"),
        ],
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope", "arguments": {"q": ARG}}}),
    )
    .await
    .unwrap();
    // refused before the credential checked out: no span
    legacy(n.addr, "rmk_wrong_but_long_enough", "tools/list", json!({})).await;
    n.app.otel.flush().await;

    let seen = seen.lock().unwrap().clone();
    let all: String = seen.iter().map(|(_, _, b)| b.as_str()).collect();
    for (path, headers, _) in &seen {
        assert!(path == "/v1/traces" || path == "/v1/metrics", "{path}");
        assert_eq!(headers["x-otlp-key"], "col-s3cr3t-key");
        assert_eq!(headers["content-type"], "application/json");
    }
    // nothing that must stay inside: an argument value, the result that echoes it, the keys, the collector header
    for secret in [
        ARG,
        MCP_KEY,
        ADMIN_KEY,
        "col-s3cr3t-key",
        "Bearer",
        "read me",
        "rmk_wrong",
    ] {
        assert!(!all.contains(secret), "{secret} was exported");
    }
    let spans: Vec<Value> = seen
        .iter()
        .filter(|(p, _, _)| p == "/v1/traces")
        .flat_map(|(_, _, b)| {
            let v: Value = serde_json::from_str(b).unwrap();
            let rs = &v["resourceSpans"][0];
            assert_eq!(attr(&rs["resource"], "service.name"), Some("ramen-node"));
            assert_eq!(
                attr(&rs["resource"], "service.version"),
                Some(env!("CARGO_PKG_VERSION"))
            );
            assert_eq!(attr(&rs["resource"], "ramen.group"), Some("demo"));
            assert_eq!(attr(&rs["resource"], "ramen.zone"), Some("a"));
            rs["scopeSpans"][0]["spans"].as_array().unwrap().clone()
        })
        .collect();
    let server: Vec<&Value> = spans.iter().filter(|s| s["kind"] == 2).collect();
    assert_eq!(
        server.len(),
        5,
        "one SERVER span per authenticated call: {spans:?}"
    );
    let echo = server
        .iter()
        .find(|s| attr(s, "mcp.name") == Some("echo"))
        .expect("the echo call");
    assert_eq!(echo["name"], "mcp tools/call");
    assert_eq!(echo["traceId"], "0af7651916cd43dd8448eb211c80319c");
    assert_eq!(echo["parentSpanId"], "b7ad6b7169203331");
    for (k, v) in [
        ("mcp.method", "tools/call"),
        ("mcp.protocol", V),
        ("ramen.group", "demo"),
        ("ramen.zone", "a"),
        ("ramen.env", "prod"),
        ("ramen.transport", "http"),
        ("ramen.status", "ok"),
    ] {
        assert_eq!(attr(echo, k), Some(v), "{k}");
    }
    let key_id = attr(echo, "ramen.key_id").expect("the key id");
    assert!(key_id.len() == 8 && !MCP_KEY.contains(key_id));
    assert!(attr(echo, "ramen.reason").is_none());
    // the sidecar round trip is its child
    let child = spans
        .iter()
        .find(|s| s["name"] == "runtime call_tool" && s["parentSpanId"] == echo["spanId"])
        .expect("the runtime child span");
    assert_eq!(
        (&child["traceId"], &child["kind"]),
        (&echo["traceId"], &json!(1))
    );
    assert!(spans.iter().any(|s| s["name"] == "runtime read_resource"));
    // a denial carries its reason; a call without a traceparent starts its own trace
    let blocked = server
        .iter()
        .find(|s| attr(s, "mcp.name") == Some("secret_tool"))
        .unwrap();
    assert_eq!(
        (attr(blocked, "ramen.status"), attr(blocked, "ramen.reason")),
        (Some("denied"), Some("blocked_name"))
    );
    assert!(blocked.get("parentSpanId").is_none());
    assert_ne!(blocked["traceId"], echo["traceId"]);
    let grpc = server
        .iter()
        .find(|s| attr(s, "ramen.transport") == Some("grpc"))
        .unwrap();
    assert_eq!(
        (attr(grpc, "ramen.status"), &grpc["status"]["code"]),
        (Some("error"), &json!(2))
    );
    assert_eq!(
        (&grpc["traceId"], &grpc["parentSpanId"], &grpc["traceState"]),
        (
            &json!("1bf7651916cd43dd8448eb211c80319c"),
            &json!("c7ad6b7169203331"),
            &json!("vendor=1")
        )
    );

    // counters, by method / name / status
    let (_, _, body) = seen.iter().rfind(|(p, _, _)| p == "/v1/metrics").unwrap();
    let v: Value = serde_json::from_str(body).unwrap();
    let metrics = v["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        .as_array()
        .unwrap();
    let points = |name: &str| -> Vec<Value> {
        metrics.iter().find(|m| m["name"] == name).unwrap()["sum"]["dataPoints"]
            .as_array()
            .unwrap()
            .clone()
    };
    let calls = points("ramen.mcp.calls");
    let total: u64 = calls
        .iter()
        .map(|p| p["asInt"].as_str().unwrap().parse::<u64>().unwrap())
        .sum();
    assert_eq!(total, 5);
    assert!(
        calls
            .iter()
            .any(|p| attr(p, "mcp.method") == Some("tools/list")
                && attr(p, "ramen.status") == Some("ok")
                && attr(p, "mcp.name").is_none())
    );
    let errors = points("ramen.mcp.errors");
    assert_eq!(errors.len(), 2, "{errors:?}");
    assert!(errors.iter().all(|p| attr(p, "ramen.status") != Some("ok")));
}

#[tokio::test]
async fn a_dead_collector_never_fails_or_slows_a_call() {
    let n = worker(&[("RAMEN_OTEL_ENDPOINT", "http://127.0.0.1:1")]).await;
    let t0 = std::time::Instant::now();
    for _ in 0..5 {
        let r = legacy(
            n.addr,
            MCP_KEY,
            "tools/call",
            json!({"name": "echo", "arguments": {}}),
        )
        .await;
        assert!(r.json()["result"]["content"].is_array(), "{}", r.body);
    }
    assert!(t0.elapsed() < Duration::from_secs(10));
    n.app.otel.flush().await;
    // and with no endpoint, nothing is even collected
    let off = worker(&[]).await;
    assert!(!off.app.otel.enabled());
}

// ------------------------------------------------------------------------------------ §26.3 telemetry through Ramen

const VERSION: &str = "0f1e2d3c4b5a";

fn report(version: &str, events: Value) -> Value {
    json!({"_meta": {"io.ramen/telemetry": {"version": version, "seq": 3, "events": events}}})
}

/// A report with nothing in it, for a worker that was never told a version.
fn empty_report() -> Value {
    report("", json!([]))
}

fn tel_token(sub: &str) -> String {
    let key = ramen_node::session::Sessions::new(Some("s"), 60).derived_key("oauth");
    ramen_node::token::mint(
        &json!({"iss": "https://console.example", "sub": sub, "email": "ada@example.com", "aud": "mcp:demo:a",
                "scope": "mcp:demo:a", "exp": ramen_node::session::now() + 600, "tel": true}),
        &key,
    )
}

async fn reporting_worker(collector: SocketAddr) -> Node {
    let endpoint = format!("http://{collector}");
    let client = format!(
        r#"{{"otlp_endpoint": "{endpoint}", "headers": {{}}, "version": "{VERSION}", "required": true}}"#
    );
    let mut vars = CONSENT.to_vec();
    vars.extend([
        ("RAMEN_OTEL_ENDPOINT", endpoint.as_str()),
        ("RAMEN_OTEL_CLIENT", client.as_str()),
        ("RAMEN_TELEMETRY_VERSION", VERSION),
        ("RAMEN_ENV", "prod"),
        ("RAMEN_VERBOSE", "1"), // the access log quotes the request — minus the report
    ]);
    worker(&vars).await
}

fn echo() -> Value {
    json!({"name": "echo", "arguments": {"q": "t"}})
}

#[tokio::test]
async fn a_gated_call_carries_a_fresh_report_whose_events_become_client_spans() {
    log_path();
    let (collector, seen, _) = recorder(200).await;
    let n = reporting_worker(collector).await;
    let t = tel_token("u-rep");
    const REASONING: &str = "the-model-thought-about-it-7e3a9c";
    let events = json!([
        {"ts": "2026-11-14T10:00:00Z", "kind": "llm", "name": "gpt-x", "ms": 1200, "ok": true,
         "reasoning": format!("{REASONING}{}", "x".repeat(5000))},
        {"ts": "2026-11-14T10:00:01.250+01:00", "kind": "tool", "name": "shell", "ms": 40, "ok": false}
    ]);

    // without the report: -32024, HTTP 428, the version to report with
    let r = modern(n.addr, &t, json!({}), "tools/call", echo(), &[]).await;
    assert_eq!((r.status, r.code()), (428, Some(-32024)), "{}", r.body);
    assert_eq!(r.json()["error"]["message"], "telemetry report required");
    assert_eq!(r.json()["error"]["data"]["version"], VERSION);
    let line = log_line(|v| v["reason"] == "telemetry_required").expect("logged");
    assert_eq!(line["status"], "denied");
    // a stale version: -32025, nothing of it forwarded
    let r = modern(
        n.addr,
        &t,
        json!({}),
        "tools/call",
        with(echo(), report("deadbeef0000", events.clone())),
        &[],
    )
    .await;
    assert_eq!((r.status, r.code()), (428, Some(-32025)), "{}", r.body);
    assert_eq!(
        r.json()["error"]["message"],
        "telemetry config changed: sign in again"
    );
    assert_eq!(r.json()["error"]["data"]["version"], VERSION);
    assert!(log_line(|v| v["reason"] == "telemetry_stale").is_some());
    // limits: 200 events, 64 KiB
    let many: Vec<Value> = (0..201)
        .map(|_| json!({"ts": "2026-11-14T10:00:00Z", "kind": "mcp", "name": "x", "ms": 1, "ok": true}))
        .collect();
    let big = json!([{"ts": "2026-11-14T10:00:00Z", "kind": "other", "name": "x", "ms": 1, "ok": true,
                      "reasoning": "r".repeat(70_000)}]);
    for bad in [json!(many), big, json!("nope")] {
        let r = modern(
            n.addr,
            &t,
            json!({}),
            "tools/call",
            with(echo(), report(VERSION, bad)),
            &[],
        )
        .await;
        assert_eq!((r.status, r.code()), (200, Some(-32602)), "{}", r.body);
    }
    // a 2025-06-18 client is told what to use instead
    let r = legacy(n.addr, &t, "tools/call", echo()).await;
    assert_eq!((r.status, r.code()), (200, Some(-32003)), "{}", r.body);
    assert_eq!(
        r.json()["error"]["message"],
        "forbidden: this group requires telemetry; use a client that reports it (MCP 2026-07-28 or the Ramen bridge)"
    );
    // the console's own calls are not asked
    let auth = format!("Bearer {MCP_KEY}");
    let r = post(
        n.addr,
        &[("Authorization", &auth), ("x-ramen-admin-key", ADMIN_KEY)],
        &json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": echo()}).to_string(),
    )
    .await;
    assert!(r.text().contains(r#""q": "t""#), "{}", r.body);
    // lists are not gated
    let r = modern(n.addr, &t, json!({}), "tools/list", json!({}), &[]).await;
    assert_eq!(r.json()["result"]["resultType"], "complete", "{}", r.body);

    // the report accepted: the call runs, the tool never sees the report
    let r = modern(
        n.addr,
        &t,
        json!({}),
        "tools/call",
        with(echo(), report(VERSION, events.clone())),
        &[],
    )
    .await;
    assert_eq!(r.status, 200, "{}", r.body);
    assert!(r.text().contains(r#""q": "t""#), "{}", r.body);
    assert!(!r.body.contains("io.ramen/telemetry"), "{}", r.body);
    // `events: []` is a report too; and over gRPC the same gate
    let r = modern(
        n.addr,
        &t,
        json!({}),
        "resources/read",
        with(
            json!({"uri": "ramen://demo/readme"}),
            report(VERSION, json!([])),
        ),
        &[],
    )
    .await;
    assert_eq!(r.json()["result"]["resultType"], "complete", "{}", r.body);
    let bearer = format!("Bearer {t}");
    let md = [("authorization", bearer.as_str())];
    let g = grpc_call(
        n.addr,
        &md,
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": with(echo(), json!({"_meta": meta(json!({}))}))}),
    )
    .await
    .unwrap();
    assert_eq!(g["error"]["code"], -32024, "{g}");
    let mut p = with(echo(), json!({"_meta": meta(json!({}))}));
    p["_meta"]["io.ramen/telemetry"] =
        report(VERSION, events.clone())["_meta"]["io.ramen/telemetry"].clone();
    let g = grpc_call(
        n.addr,
        &md,
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": p}),
    )
    .await
    .unwrap();
    assert_eq!(g["result"]["resultType"], "complete", "{g}");

    // `telemetry/report` between calls: modern, authenticated, never consent-gated (an rmk key, nothing recorded)
    let r = modern(
        n.addr,
        MCP_KEY,
        json!({}),
        "telemetry/report",
        report(VERSION, events.clone())["_meta"]["io.ramen/telemetry"].clone(),
        &[],
    )
    .await;
    assert_eq!(r.status, 200, "{}", r.body);
    assert_eq!(r.json()["result"]["accepted"], 2, "{}", r.body);
    assert_eq!(r.json()["result"]["version"], VERSION);
    assert_eq!(r.json()["result"]["resultType"], "complete");
    let r = modern(
        n.addr,
        MCP_KEY,
        json!({}),
        "telemetry/report",
        json!({"version": "old", "seq": 1, "events": []}),
        &[],
    )
    .await;
    assert_eq!((r.status, r.code()), (428, Some(-32025)), "{}", r.body);
    let r = modern(
        n.addr,
        MCP_KEY,
        json!({}),
        "telemetry/report",
        json!({"version": VERSION, "seq": 1}),
        &[],
    )
    .await;
    assert_eq!(r.code(), Some(-32602), "{}", r.body);
    let r = legacy(
        n.addr,
        MCP_KEY,
        "telemetry/report",
        json!({"version": VERSION, "seq": 1, "events": []}),
    )
    .await;
    assert_eq!(r.code(), Some(-32601), "{}", r.body);

    // nothing of the events reaches the access log, verbose or not
    let log = std::fs::read_to_string(log_path()).unwrap();
    assert!(!log.contains(REASONING) && !log.contains("gpt-x"), "{log}");

    // the events are CLIENT spans under the call's span, with the fixed attributes
    n.app.otel.flush().await;
    let seen = seen.lock().unwrap().clone();
    let spans: Vec<Value> = seen
        .iter()
        .filter(|(p, _, _)| p == "/v1/traces")
        .flat_map(|(_, _, b)| {
            serde_json::from_str::<Value>(b).unwrap()["resourceSpans"][0]["scopeSpans"][0]["spans"]
                .as_array()
                .unwrap()
                .clone()
        })
        .collect();
    let client: Vec<&Value> = spans.iter().filter(|s| s["kind"] == 3).collect();
    // the HTTP call, the gRPC call, the report: 2 events each; the stale report and `events: []`: none
    assert_eq!(client.len(), 6, "{client:?}");
    let llm = client
        .iter()
        .find(|s| s["name"] == "client llm gpt-x" && attr(s, "ramen.transport") == Some("http"))
        .expect("the llm event");
    let parent = spans
        .iter()
        .find(|s| s["spanId"] == llm["parentSpanId"])
        .expect("under the call's span");
    assert_eq!(parent["name"], "mcp tools/call");
    assert_eq!(attr(parent, "mcp.name"), Some("echo"));
    assert_eq!(llm["traceId"], parent["traceId"]);
    assert_eq!(llm["startTimeUnixNano"], "1794650400000000000");
    assert_eq!(llm["endTimeUnixNano"], "1794650401200000000");
    assert!(llm.get("status").is_none());
    for (k, v) in [
        ("ramen.client.ip", "127.0.0.1"),
        ("ramen.user", "ada@example.com"),
        ("ramen.key_id", "user:u-rep"),
        ("ramen.group", "demo"),
        ("ramen.zone", "a"),
        ("ramen.env", "prod"),
        ("ramen.worker", ramen_node::telemetry::worker()),
        ("ramen.event.kind", "llm"),
        ("ramen.event.seq", "3"),
        ("ramen.transport", "http"),
    ] {
        assert_eq!(attr(llm, k), Some(v), "{k}");
    }
    let reasoning = attr(llm, "ramen.reasoning").unwrap();
    assert!(reasoning.starts_with(REASONING) && reasoning.len() == 4096);
    let tool = client
        .iter()
        .find(|s| s["name"] == "client tool shell" && attr(s, "ramen.transport") == Some("grpc"))
        .unwrap();
    assert_eq!(tool["status"]["code"], 2);
    assert_eq!(tool["startTimeUnixNano"], "1794646801250000000");
    assert!(attr(tool, "ramen.reasoning").is_none());
    // an rmk key: the user is the key id
    let by_key = client
        .iter()
        .find(|s| attr(s, "ramen.key_id") != Some("user:u-rep"))
        .unwrap();
    assert_eq!(attr(by_key, "ramen.user"), attr(by_key, "ramen.key_id"));
    assert!(spans.iter().any(|s| s["name"] == "mcp telemetry/report"));
    // counted by kind / ok
    let (_, _, body) = seen.iter().rfind(|(p, _, _)| p == "/v1/metrics").unwrap();
    let v: Value = serde_json::from_str(body).unwrap();
    let metric = v["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
        .as_array()
        .unwrap()
        .iter()
        .find(|m| m["name"] == "ramen.client.events")
        .expect("the counter")
        .clone();
    let points = metric["sum"]["dataPoints"].as_array().unwrap();
    let count = |kind: &str, ok: &str| {
        points
            .iter()
            .find(|p| {
                attr(p, "ramen.event.kind") == Some(kind) && attr(p, "ramen.event.ok") == Some(ok)
            })
            .map(|p| p["asInt"].as_str().unwrap().to_string())
    };
    assert_eq!(count("llm", "true").as_deref(), Some("3"), "{points:?}");
    assert_eq!(count("tool", "false").as_deref(), Some("3"));
    // the whole export never quotes the keys
    let all: String = seen.iter().map(|(_, _, b)| b.as_str()).collect();
    assert!(!all.contains(MCP_KEY) && !all.contains(ADMIN_KEY));
}

#[tokio::test]
async fn a_reload_makes_a_pushed_telemetry_config_live() {
    let (c1, seen1, _) = recorder(200).await;
    let (c2, seen2, _) = recorder(200).await;
    let cfg_for = |collector: SocketAddr, version: &str| {
        let endpoint = format!("http://{collector}");
        let client = format!(
            r#"{{"otlp_endpoint": "{endpoint}", "headers": {{}}, "version": "{version}", "required": true}}"#
        );
        // the reload re-sets the process-global log file: keep this process's
        let log = log_path().display().to_string();
        let mut vars = CONSENT.to_vec();
        vars.extend([
            ("RAMEN_OTEL_ENDPOINT", endpoint.as_str()),
            ("RAMEN_OTEL_CLIENT", client.as_str()),
            ("RAMEN_TELEMETRY_VERSION", version),
            ("RAMEN_LOG_FILE", log.as_str()),
        ]);
        config(&vars)
    };
    let live = Arc::new(Mutex::new(cfg_for(c1, "v1-aaaaaaaaaa")));
    let source = live.clone();
    let first = live.lock().unwrap().clone();
    let n = serve_from(
        first,
        Box::new(move || Ok(source.lock().unwrap().clone())),
        TcpListener::bind("127.0.0.1:0").await.unwrap(),
    )
    .await;
    let t = tel_token("u-reload");
    let event = json!([{"ts": "2026-11-14T10:00:00Z", "kind": "mcp", "name": "tools/list", "ms": 5, "ok": true}]);
    let call = |version: &str| {
        modern(
            n.addr,
            &t,
            json!({}),
            "tools/call",
            with(echo(), report(version, event.clone())),
            &[],
        )
    };
    assert_eq!(call("v1-aaaaaaaaaa").await.status, 200);
    n.app.otel.flush().await; // export is batched: what was reported so far goes to the old collector
    // the console pushes a new config and reloads
    *live.lock().unwrap() = cfg_for(c2, "v2-bbbbbbbbbb");
    let mut admin = AdminClient::connect(format!("http://{}", n.addr))
        .await
        .unwrap();
    let mut req = tonic::Request::new(ReloadRequest {});
    req.metadata_mut()
        .insert("x-ramen-admin-key", ADMIN_KEY.parse().unwrap());
    admin.reload(req).await.unwrap();
    let r = call("v1-aaaaaaaaaa").await;
    assert_eq!((r.status, r.code()), (428, Some(-32025)), "{}", r.body);
    assert_eq!(r.json()["error"]["data"]["version"], "v2-bbbbbbbbbb");
    assert_eq!(call("v2-bbbbbbbbbb").await.status, 200);
    let r = modern(n.addr, &t, json!({}), "server/discover", json!({}), &[]).await;
    let tel = &r.json()["result"]["_meta"]["io.ramen/telemetry"];
    assert_eq!(tel["version"], "v2-bbbbbbbbbb", "{}", r.body);
    assert_eq!(tel["required"], true);
    assert_eq!(tel["otlp_endpoint"], format!("http://{c2}"));
    // and the events now go to the new collector
    n.app.otel.flush().await;
    let client_spans = |seen: &Seen| -> usize {
        seen.lock()
            .unwrap()
            .iter()
            .filter(|(p, _, _)| p == "/v1/traces")
            .flat_map(|(_, _, b)| {
                serde_json::from_str::<Value>(b).unwrap()["resourceSpans"][0]["scopeSpans"][0]["spans"]
                    .as_array()
                    .unwrap()
                    .clone()
            })
            .filter(|s| s["kind"] == 3)
            .count()
    };
    assert_eq!((client_spans(&seen1), client_spans(&seen2)), (1, 1));
}

/// Amendment to §26.3: the bridge is a raw passthrough in front of 2025-06-18 hosts and puts the report into the
/// legacy body — such a call is served and forwarded like a modern one; only a legacy call without one is -32003.
#[tokio::test]
async fn a_legacy_body_carrying_the_report_is_served_and_forwarded() {
    let (collector, seen, _) = recorder(200).await;
    let n = reporting_worker(collector).await;
    let t = tel_token("u-legacy");
    let event = json!([{"ts": "2026-11-14T10:00:00Z", "kind": "mcp", "name": "tools/list", "ms": 5, "ok": true,
                        "reasoning": "legacy-thought-5b1c"}]);
    let r = legacy(
        n.addr,
        &t,
        "tools/call",
        with(echo(), report(VERSION, event.clone())),
    )
    .await;
    assert_eq!(r.status, 200, "{}", r.body);
    assert!(r.text().contains(r#""q": "t""#), "{}", r.body);
    // stale: -32025, HTTP 200 on this era (§23.1)
    let r = legacy(
        n.addr,
        &t,
        "tools/call",
        with(echo(), report("old-version00", event)),
    )
    .await;
    assert_eq!((r.status, r.code()), (200, Some(-32025)), "{}", r.body);
    assert_eq!(r.json()["error"]["data"]["version"], VERSION);
    // without: -32003
    let r = legacy(n.addr, &t, "tools/call", echo()).await;
    assert_eq!((r.status, r.code()), (200, Some(-32003)), "{}", r.body);
    n.app.otel.flush().await;
    let seen = seen.lock().unwrap().clone();
    let spans: Vec<Value> = seen
        .iter()
        .filter(|(p, _, _)| p == "/v1/traces")
        .flat_map(|(_, _, b)| {
            serde_json::from_str::<Value>(b).unwrap()["resourceSpans"][0]["scopeSpans"][0]["spans"]
                .as_array()
                .unwrap()
                .clone()
        })
        .collect();
    let client: Vec<&Value> = spans.iter().filter(|s| s["kind"] == 3).collect();
    assert_eq!(client.len(), 1, "the fresh report's event only: {client:?}");
    assert_eq!(client[0]["name"], "client mcp tools/list");
    assert_eq!(
        attr(client[0], "ramen.reasoning"),
        Some("legacy-thought-5b1c")
    );
    let parent = spans
        .iter()
        .find(|s| s["spanId"] == client[0]["parentSpanId"])
        .unwrap();
    assert_eq!(attr(parent, "mcp.protocol"), Some("2025-06-18"));
    let log = std::fs::read_to_string(log_path()).unwrap_or_default();
    assert!(!log.contains("legacy-thought-5b1c"));
}
