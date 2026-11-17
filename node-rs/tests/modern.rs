//! MCP 2026-07-28 over the real transports (CONTRACTS §23), against a stand-in runtime that speaks the sidecar
//! protocol (`tests/fake_runtime`). Raw HTTP/1.1 over a socket, as in `http.rs`'s tests: nothing between the test
//! and the wire. The legacy era is probed beside the modern one wherever a change could have leaked into it.
use ramen_node::config::Config;
use ramen_node::grpc::{Shared, app_with, routes};
use ramen_node::pb::JsonRpc;
use ramen_node::pb::mcp_client::McpClient;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::Ordering::Relaxed;
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

const V: &str = "2026-07-28";
const TASKS: &str = "io.modelcontextprotocol/tasks";
const SUB: &str = "io.modelcontextprotocol/subscriptionId";
const SSE: (&str, &str) = ("Accept", "application/json, text/event-stream");

struct Node {
    app: Shared,
    addr: SocketAddr,
    bucket: PathBuf,
    _stop: tokio::sync::oneshot::Sender<()>,
}

/// A node on a fresh bucket dir with the fake runtime loaded. Keys `k1` and `k2`.
async fn node(extra: &[(&str, &str)]) -> Node {
    let fake = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fake_runtime");
    let nonce: String = ramen_node::session::random(6)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    let bucket = std::env::temp_dir().join(format!("ramen-modern-{nonce}"));
    std::fs::create_dir_all(&bucket).unwrap();
    let mut m: HashMap<String, String> = [
        ("RAMEN_MCP_KEYS", "k1,k2"),
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
    let cfg = Config::from_map(&m).unwrap();
    let again = cfg.clone();
    let app = app_with(cfg, Box::new(move || Ok(again.clone()))).await;
    app.tasks.poll_ms.store(40, Relaxed);
    app.load().await.expect("the fake runtime loads");
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
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
        bucket,
        _stop: tx,
    }
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
    /// The `data:` payloads of an SSE body (chunk framing lines fall away by themselves).
    fn events(&self) -> Vec<Value> {
        events_of(&self.body)
    }
    fn code(&self) -> Option<i64> {
        self.json()["error"]["code"].as_i64()
    }
}

fn events_of(text: &str) -> Vec<Value> {
    text.lines()
        .filter_map(|l| l.strip_prefix("data: "))
        .filter_map(|d| serde_json::from_str(d.trim()).ok())
        .collect()
}

fn head(text: &str) -> (u16, Vec<(String, String)>) {
    let mut lines = text.lines();
    let status = lines
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let headers = lines
        .filter_map(|l| l.split_once(':'))
        .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
        .collect();
    (status, headers)
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
    let (status, headers) = head(h);
    Reply {
        status,
        headers,
        body: b.to_string(),
    }
}

fn bearer(key: &str) -> String {
    format!("Bearer {key}")
}

fn meta(caps: Value) -> Value {
    json!({"io.modelcontextprotocol/protocolVersion": V, "io.modelcontextprotocol/clientCapabilities": caps,
           "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "0"}})
}

fn target(method: &str, params: &Value) -> Option<String> {
    let field = match method {
        "tools/call" | "prompts/get" => "name",
        "resources/read" => "uri",
        "tasks/get" | "tasks/update" | "tasks/cancel" => "taskId",
        _ => return None,
    };
    params.get(field).and_then(Value::as_str).map(String::from)
}

/// A well-formed 2026-07-28 request as `key`, declaring `caps`: standard headers set from the body.
async fn modern_as(
    n: &Node,
    key: &str,
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
    let auth = bearer(key);
    let name = target(method, &params);
    let mut hs = vec![
        ("Authorization", auth.as_str()),
        ("MCP-Protocol-Version", V),
        ("Mcp-Method", method),
    ];
    if let Some(nm) = &name {
        hs.push(("Mcp-Name", nm));
    }
    // an extra header replaces the helper's own of the same name
    hs.retain(|(k, _)| !extra.iter().any(|(e, _)| e.eq_ignore_ascii_case(k)));
    hs.extend_from_slice(extra);
    let body = json!({"jsonrpc": "2.0", "id": 7, "method": method, "params": params});
    post(n.addr, &hs, &body.to_string()).await
}

async fn modern(n: &Node, method: &str, params: Value) -> Reply {
    modern_as(n, "k1", json!({}), method, params, &[]).await
}

async fn legacy(n: &Node, method: &str, params: Value) -> Reply {
    let body = json!({"jsonrpc": "2.0", "id": 3, "method": method, "params": params});
    post(n.addr, &[("Authorization", "Bearer k1")], &body.to_string()).await
}

fn tool_text(v: &Value) -> String {
    v["result"]["content"][0]["text"]
        .as_str()
        .unwrap_or("")
        .to_string()
}

/// All tests share one process and so one log file (the log sink is process-global); lines are found by content.
fn log_path() -> PathBuf {
    static PATH: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();
    PATH.get_or_init(|| {
        let p = std::env::temp_dir().join(format!("ramen-modern-{}.log", std::process::id()));
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

/// §23.1: the envelope checks, their order, their HTTP statuses, and the request id on every error.
#[tokio::test]
async fn the_modern_envelope_is_checked_in_order_with_the_contract_statuses() {
    let n = node(&[]).await;
    let auth = ("Authorization", "Bearer k1");
    let send = |hs: Vec<(&'static str, &'static str)>, body: Value| {
        let addr = n.addr;
        async move {
            let mut all = vec![auth];
            all.extend(hs);
            post(addr, &all, &body.to_string()).await
        }
    };
    let discover = |id: i64, m: Value| json!({"jsonrpc": "2.0", "id": id, "method": "server/discover", "params": m});
    let std_h = vec![
        ("MCP-Protocol-Version", V),
        ("Mcp-Method", "server/discover"),
    ];
    // 1. `_meta` missing or incomplete: -32602, 400 — even though the header is right
    let full = meta(json!({}));
    let mut no_version = full.clone();
    no_version
        .as_object_mut()
        .unwrap()
        .remove("io.modelcontextprotocol/protocolVersion");
    let mut no_caps = full.clone();
    no_caps
        .as_object_mut()
        .unwrap()
        .remove("io.modelcontextprotocol/clientCapabilities");
    for (id, params) in [
        (101, json!({})),
        (102, json!({"_meta": no_version})),
        (104, json!({"_meta": no_caps})),
    ] {
        let r = send(std_h.clone(), discover(id, params)).await;
        assert_eq!((r.status, r.code()), (400, Some(-32602)), "{}", r.body);
        assert_eq!(r.json()["id"], id);
    }
    // clientInfo is optional
    let mut no_info = full.clone();
    no_info
        .as_object_mut()
        .unwrap()
        .remove("io.modelcontextprotocol/clientInfo");
    let r = send(std_h.clone(), discover(105, json!({"_meta": no_info}))).await;
    assert_eq!(
        (r.status, r.json()["error"].is_null()),
        (200, true),
        "{}",
        r.body
    );
    // 2. header missing or different from the body: -32020, before the version is judged
    let mut v999 = full.clone();
    v999["io.modelcontextprotocol/protocolVersion"] = json!("v999.0.0");
    let r = send(std_h.clone(), discover(302, json!({"_meta": v999}))).await;
    assert_eq!(
        (r.status, r.code(), r.json()["id"].as_i64()),
        (400, Some(-32020), Some(302))
    );
    let r = send(
        vec![("Mcp-Method", "server/discover")],
        discover(303, json!({"_meta": full})),
    )
    .await;
    assert_eq!((r.status, r.code()), (400, Some(-32020)));
    // 3. both name a version we do not speak: -32022 with what we do
    let r = send(
        vec![
            ("MCP-Protocol-Version", "v999.0.0"),
            ("Mcp-Method", "server/discover"),
        ],
        discover(301, json!({"_meta": v999})),
    )
    .await;
    assert_eq!(
        (r.status, r.code(), r.json()["id"].as_i64()),
        (400, Some(-32022), Some(301))
    );
    assert_eq!(
        r.json()["error"]["data"],
        json!({"supported": [V, "2025-06-18", "2025-03-26", "2024-11-05"], "requested": "v999.0.0"})
    );
    // 5. what 2026-07-28 removed, and anything unknown: -32601 with 404
    for gone in [
        "initialize",
        "ping",
        "logging/setLevel",
        "resources/subscribe",
        "resources/unsubscribe",
        "unknown/method",
        "tasks/list",
    ] {
        let r = modern(&n, gone, json!({})).await;
        assert_eq!(
            (r.status, r.code(), r.json()["id"].as_i64()),
            (404, Some(-32601), Some(7)),
            "{gone}"
        );
    }
    // a notification is accepted and ignored
    let note = json!({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"_meta": meta(json!({}))}});
    let r = send(
        vec![
            ("MCP-Protocol-Version", V),
            ("Mcp-Method", "notifications/cancelled"),
        ],
        note,
    )
    .await;
    assert_eq!((r.status, r.body.as_str()), (202, ""));
    // the guards are the legacy guards: no key 401, foreign Origin 403, whatever the era
    let body = discover(1, json!({"_meta": meta(json!({}))})).to_string();
    let r = post(n.addr, &std_h, &body).await;
    assert_eq!(r.status, 401);
    let mut evil = std_h.clone();
    evil.extend([auth, ("Origin", "http://evil.example.com")]);
    assert_eq!(post(n.addr, &evil, &body).await.status, 403);
    // GET stays 405
    let mut s = TcpStream::connect(n.addr).await.unwrap();
    s.write_all(
        format!(
            "GET /mcp HTTP/1.1\r\nHost: x\r\nConnection: close\r\nMCP-Protocol-Version: {V}\r\n\r\n"
        )
        .as_bytes(),
    )
    .await
    .unwrap();
    let mut raw = String::new();
    s.read_to_string(&mut raw).await.unwrap();
    assert!(raw.starts_with("HTTP/1.1 405"), "{raw}");
}

/// §23.1: discover, the result envelope, the cache hints, no session — and the legacy era untouched beside it.
#[tokio::test]
async fn modern_results_carry_the_envelope_and_legacy_ones_do_not() {
    let n = node(&[("RAMEN_LIST_TTL_MS", "1234")]).await;
    let r = modern(&n, "server/discover", json!({})).await;
    assert_eq!(r.status, 200, "{}", r.body);
    let d = r.json()["result"].clone();
    assert_eq!(d["resultType"], "complete");
    assert_eq!(
        d["supportedVersions"],
        json!([V, "2025-06-18", "2025-03-26", "2024-11-05"])
    );
    assert_eq!(
        d["capabilities"],
        json!({"tools": {"listChanged": true}, "prompts": {"listChanged": true}, "resources": {"listChanged": true},
               "completions": {}, "extensions": {TASKS: {}}})
    );
    assert_eq!(
        (d["ttlMs"].as_u64(), d["cacheScope"].as_str()),
        (Some(1234), Some("private"))
    );
    assert_eq!(
        d["_meta"]["io.modelcontextprotocol/serverInfo"]["name"],
        "ramen-node"
    );
    // no session, no version echo; a session header that would be a 404 on the legacy era is not even read
    assert!(r.header("mcp-session-id").is_none() && r.header("mcp-protocol-version").is_none());
    let r = modern_as(
        &n,
        "k1",
        json!({}),
        "tools/list",
        json!({}),
        &[("Mcp-Session-Id", "garbage")],
    )
    .await;
    assert_eq!(r.status, 200, "{}", r.body);
    let l = r.json()["result"].clone();
    assert_eq!(
        (
            l["resultType"].as_str(),
            l["ttlMs"].as_u64(),
            l["cacheScope"].as_str()
        ),
        (Some("complete"), Some(1234), Some("private"))
    );
    assert!(l["tools"].as_array().unwrap().len() >= 5);
    for (m, key) in [
        ("prompts/list", "prompts"),
        ("resources/list", "resources"),
        ("resources/templates/list", "resourceTemplates"),
    ] {
        let v = modern(&n, m, json!({})).await.json()["result"].clone();
        assert!(
            v[key].is_array() && v["ttlMs"] == 1234 && v["cacheScope"] == "private",
            "{m}: {v}"
        );
    }
    let v = modern(&n, "resources/read", json!({"uri": "ramen://demo/readme"}))
        .await
        .json()["result"]
        .clone();
    assert_eq!(
        (v["ttlMs"].as_u64(), v["contents"][0]["text"].as_str()),
        (Some(0), Some("read me"))
    );
    let v = modern(
        &n,
        "tools/call",
        json!({"name": "echo", "arguments": {"q": "x"}}),
    )
    .await
    .json()["result"]
        .clone();
    assert_eq!(v["resultType"], "complete");
    assert!(
        v.get("ttlMs").is_none() && v["_meta"]["io.modelcontextprotocol/serverInfo"].is_object()
    );
    // legacy: the 0.8 answers, key for key
    let r = legacy(&n, "tools/list", json!({})).await;
    let keys: Vec<String> = r.json()["result"]
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect();
    assert_eq!(keys, vec!["tools"]);
    assert_eq!(r.header("mcp-protocol-version"), Some("2025-06-18"));
    assert_eq!(
        legacy(&n, "ping", json!({})).await.json()["result"],
        json!({})
    );
    let init = legacy(
        &n,
        "initialize",
        json!({"protocolVersion": "2025-11-25", "capabilities": {}}),
    )
    .await;
    assert_eq!(init.json()["result"]["protocolVersion"], "2025-06-18");
    assert!(init.header("mcp-session-id").is_some());
    assert_eq!(
        init.json()["result"]["capabilities"],
        json!({"tools": {"listChanged": true}, "resources": {}, "prompts": {}})
    );
    let v = legacy(
        &n,
        "tools/call",
        json!({"name": "echo", "arguments": {"q": "x"}}),
    )
    .await
    .json()["result"]
        .clone();
    let keys: Vec<String> = v.as_object().unwrap().keys().cloned().collect();
    assert_eq!(keys, vec!["content", "isError"]);
    assert_eq!(
        legacy(&n, "server/discover", json!({})).await.code(),
        Some(-32601)
    );
    assert_eq!(
        legacy(&n, "tasks/get", json!({"taskId": "x"})).await.code(),
        Some(-32601)
    );
    let r = legacy(&n, "garbage", json!({})).await;
    assert_eq!((r.status, r.code()), (200, Some(-32601)));
    // a bad session is still a 404 on the legacy era
    let r = post(
        n.addr,
        &[
            ("Authorization", "Bearer k1"),
            ("Mcp-Session-Id", "garbage"),
        ],
        r#"{"jsonrpc":"2.0","id":1,"method":"ping"}"#,
    )
    .await;
    assert_eq!(r.status, 404);
}

/// §23.1: nothing from -32000..-32019; unknown, blocked and hidden are one answer; legacy keeps its codes.
#[tokio::test]
async fn modern_error_codes_and_the_unchanged_legacy_ones() {
    let n = node(&[
        ("RAMEN_BLOCKED", "secret_tool,vault"),
        (
            "RAMEN_TOOL_ACCESS",
            r#"{"prog":{"list":[],"call":[]},"ask":{"list":["key"],"call":[]}}"#,
        ),
    ])
    .await;
    for name in ["nope", "secret_tool", "prog"] {
        let r = modern(&n, "tools/call", json!({"name": name, "arguments": {}})).await;
        assert_eq!(r.status, 200, "{name}");
        assert_eq!(
            r.json()["error"],
            json!({"code": -32602, "message": format!("Unknown tool: {name}")}),
            "{name}"
        );
    }
    let r = modern(&n, "tools/call", json!({"name": "ask", "arguments": {}})).await;
    assert_eq!(
        r.json()["error"],
        json!({"code": -32602, "message": "forbidden: ask is not callable for key"})
    );
    let r = modern(&n, "prompts/get", json!({"name": "nope"})).await;
    assert_eq!((r.status, r.code()), (200, Some(-32602)));
    for uri in ["ramen://nowhere", "ramen://vault/1"] {
        let r = modern(&n, "resources/read", json!({"uri": uri})).await;
        assert_eq!(
            r.json()["error"],
            json!({"code": -32602, "message": "Resource not found", "data": {"uri": uri}})
        );
    }
    // the blocked template is not listed either, and a readable one still reads
    let t = modern(&n, "resources/templates/list", json!({}))
        .await
        .json()["result"]["resourceTemplates"]
        .clone();
    assert_eq!(t.as_array().unwrap().len(), 1, "{t}");
    assert_eq!(t[0]["uriTemplate"], "ramen://items/{id}/data");
    let r = modern(
        &n,
        "resources/read",
        json!({"uri": "ramen://items/123/data"}),
    )
    .await;
    assert!(
        r.json()["result"]["contents"][0]["text"]
            .as_str()
            .unwrap()
            .contains("123"),
        "{}",
        r.body
    );
    // the log reasons did not change
    log_path();
    let _ = modern(
        &n,
        "tools/call",
        json!({"name": "secret_tool", "arguments": {}}),
    )
    .await;
    let line = log_line(|v| v["name"] == "secret_tool").expect("a line");
    assert_eq!(
        (line["reason"].as_str(), line["protocol"].as_str()),
        (Some("blocked_name"), Some(V))
    );
    // legacy: exactly the 0.8 codes
    assert_eq!(
        legacy(&n, "tools/call", json!({"name": "secret_tool"}))
            .await
            .code(),
        Some(-32601)
    );
    assert_eq!(
        legacy(&n, "tools/call", json!({"name": "prog"}))
            .await
            .json()["error"]["message"],
        "tool not found: prog"
    );
    assert_eq!(
        legacy(&n, "tools/call", json!({"name": "ask"}))
            .await
            .code(),
        Some(-32003)
    );
    assert_eq!(
        legacy(&n, "tools/call", json!({"name": "nope"}))
            .await
            .code(),
        Some(-32004)
    );
    assert_eq!(
        legacy(&n, "resources/read", json!({"uri": "ramen://nowhere"}))
            .await
            .code(),
        Some(-32004)
    );
    assert_eq!(
        legacy(&n, "resources/read", json!({"uri": "ramen://vault/1"}))
            .await
            .code(),
        Some(-32601)
    );
}

/// §23.2: the standard headers mirror the body or the request is refused with -32020, logged `header_mismatch`.
#[tokio::test]
async fn standard_headers_must_mirror_the_body() {
    let n = node(&[("RAMEN_BLOCKED", "secret_tool")]).await;
    log_path();
    let auth = ("Authorization", "Bearer k1");
    let call = |hs: Vec<(&'static str, &'static str)>, method: &'static str, params: Value| {
        let addr = n.addr;
        async move {
            let mut p = params;
            p["_meta"] = meta(json!({}));
            let mut all = vec![auth, ("MCP-Protocol-Version", V)];
            all.extend(hs);
            post(
                addr,
                &all,
                &json!({"jsonrpc": "2.0", "id": 9, "method": method, "params": p}).to_string(),
            )
            .await
        }
    };
    let ok = |r: &Reply| (r.status, r.json()["error"].is_null());
    let bad = |r: &Reply| (r.status, r.code(), r.json()["id"].as_i64());
    let refused = (400, Some(-32020), Some(9));
    assert_eq!(
        ok(&call(vec![("Mcp-Method", "tools/list")], "tools/list", json!({})).await),
        (200, true)
    );
    assert_eq!(
        ok(&call(vec![("mcp-method", "tools/list")], "tools/list", json!({})).await),
        (200, true)
    );
    assert_eq!(
        ok(&call(vec![("MCP-METHOD", "tools/list")], "tools/list", json!({})).await),
        (200, true)
    );
    assert_eq!(bad(&call(vec![], "tools/list", json!({})).await), refused);
    assert_eq!(
        bad(&call(
            vec![("Mcp-Method", "prompts/list")],
            "tools/list",
            json!({})
        )
        .await),
        refused
    );
    assert_eq!(
        bad(&call(vec![("Mcp-Method", "TOOLS/LIST")], "tools/list", json!({})).await),
        refused
    );
    let echo = || json!({"name": "echo", "arguments": {"q": "x"}});
    let m = ("Mcp-Method", "tools/call");
    assert_eq!(bad(&call(vec![m], "tools/call", echo()).await), refused);
    assert_eq!(
        bad(&call(
            vec![m, ("Mcp-Name", "wrong_tool_name")],
            "tools/call",
            echo()
        )
        .await),
        refused
    );
    assert_eq!(
        ok(&call(vec![m, ("Mcp-Name", "  echo  ")], "tools/call", echo()).await),
        (200, true)
    );
    assert_eq!(
        ok(&call(
            vec![m, ("Mcp-Name", "=?base64?ZWNobw==?=")],
            "tools/call",
            echo()
        )
        .await),
        (200, true)
    );
    let line = log_line(|v| v["reason"] == "header_mismatch").expect("a refused line");
    assert_eq!(
        (line["status"].as_str(), line["protocol"].as_str()),
        (Some("denied"), Some(V))
    );
    // Mcp-Param-*: required when the argument is in the body, compared after decoding
    let with = |args: Value| json!({"name": "echo", "arguments": args});
    let n_ = ("Mcp-Name", "echo");
    assert_eq!(
        bad(&call(vec![m, n_], "tools/call", with(json!({"region": "Hello"}))).await),
        refused
    );
    assert_eq!(
        ok(&call(
            vec![m, n_, ("Mcp-Param-Region", "Hello")],
            "tools/call",
            with(json!({"region": "Hello"}))
        )
        .await),
        (200, true)
    );
    assert_eq!(
        ok(&call(
            vec![m, n_, ("Mcp-Param-Region", "=?base64?SGVsbG8=?=")],
            "tools/call",
            with(json!({"region": "Hello"}))
        )
        .await),
        (200, true)
    );
    assert_eq!(
        bad(&call(
            vec![m, n_, ("Mcp-Param-Region", "=?base64?SGVsbG8?=")],
            "tools/call",
            with(json!({"region": "Hello"}))
        )
        .await),
        refused
    );
    assert_eq!(
        bad(&call(
            vec![m, n_, ("Mcp-Param-Region", "=?base64?SGVs!!!bG8=?=")],
            "tools/call",
            with(json!({"region": "Hello"}))
        )
        .await),
        refused
    );
    assert_eq!(
        ok(&call(
            vec![m, n_, ("Mcp-Param-Region", "SGVsbG8=")],
            "tools/call",
            with(json!({"region": "SGVsbG8="}))
        )
        .await),
        (200, true)
    );
    assert_eq!(
        ok(&call(
            vec![m, n_, ("Mcp-Param-Region", "=?base64?SGVsbG8=")],
            "tools/call",
            with(json!({"region": "=?base64?SGVsbG8="}))
        )
        .await),
        (200, true)
    );
    assert_eq!(
        bad(&call(
            vec![m, n_, ("Mcp-Param-Region", "other")],
            "tools/call",
            with(json!({"region": "Hello"}))
        )
        .await),
        refused
    );
    assert_eq!(
        ok(&call(
            vec![m, n_, ("Mcp-Param-Limit", "5"), ("Mcp-Param-Unknown", "z")],
            "tools/call",
            with(json!({"limit": 5}))
        )
        .await),
        (200, true)
    );
    assert_eq!(
        bad(&call(
            vec![m, n_, ("Mcp-Param-Limit", "6")],
            "tools/call",
            with(json!({"limit": 5}))
        )
        .await),
        refused
    );
    // the body stays what the tool sees
    let r = call(
        vec![m, n_, ("Mcp-Param-Region", "us")],
        "tools/call",
        with(json!({"region": "us"})),
    )
    .await;
    assert!(
        tool_text(&r.json()).contains(r#""region": "us""#),
        "{}",
        r.body
    );
    // a blocked tool answers as unknown, headers or not: nothing about its parameters leaks
    let r = call(
        vec![m, ("Mcp-Name", "secret_tool")],
        "tools/call",
        json!({"name": "secret_tool", "arguments": {}}),
    )
    .await;
    assert_eq!((r.status, r.code()), (200, Some(-32602)));
    // trace context lands on the log line, and only a well-formed one
    const TP: &str = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01";
    let _ = call(
        vec![
            ("Mcp-Method", "prompts/list"),
            ("traceparent", TP),
            ("baggage", "k=v"),
        ],
        "prompts/list",
        json!({}),
    )
    .await;
    let line = log_line(|v| v["method"] == "prompts/list" && v["trace"].is_object())
        .expect("a traced line");
    assert_eq!(line["trace"], json!({"traceparent": TP, "baggage": "k=v"}));
    let _ = call(
        vec![("Mcp-Method", "resources/list"), ("traceparent", "junk")],
        "resources/list",
        json!({}),
    )
    .await;
    assert!(
        log_line(|v| v["method"] == "resources/list" && v["transport"] == "http")
            .unwrap()
            .get("trace")
            .is_none()
    );
    // CORS: the preflight allows the new names, and the Mcp-Param-* the browser announced
    let mut s = TcpStream::connect(n.addr).await.unwrap();
    s.write_all(b"OPTIONS /mcp HTTP/1.1\r\nHost: x\r\nConnection: close\r\nAccess-Control-Request-Headers: mcp-method, Mcp-Param-Region, x-evil\r\n\r\n")
        .await
        .unwrap();
    let mut raw = String::new();
    s.read_to_string(&mut raw).await.unwrap();
    let (_, hs) = head(&raw);
    let allow = hs
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("access-control-allow-headers"))
        .unwrap()
        .1
        .clone();
    for name in [
        "Authorization",
        "Mcp-Method",
        "Mcp-Name",
        "traceparent",
        "Mcp-Param-Region",
        "Mcp-Session-Id",
    ] {
        assert!(allow.contains(name), "{name} in {allow}");
    }
    assert!(!allow.contains("x-evil"));
}

/// §23.4: one round asks, the retry answers; the state is signed for this caller and this request.
#[tokio::test]
async fn mrtr_rounds_and_the_signed_request_state() {
    let n = node(&[]).await;
    log_path();
    let elicit = json!({"elicitation": {}});
    let ask = |extra: Value| {
        let mut p = json!({"name": "ask", "arguments": {"mode": "state"}});
        p.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        p
    };
    let r = modern_as(&n, "k1", elicit.clone(), "tools/call", ask(json!({})), &[]).await;
    assert_eq!(r.status, 200, "{}", r.body);
    let first = r.json()["result"].clone();
    assert_eq!(first["resultType"], "input_required");
    assert_eq!(
        first["inputRequests"]["user_name"]["method"],
        "elicitation/create"
    );
    assert!(first.get("ttlMs").is_none() && first.get("isError").is_none());
    let state = first["requestState"]
        .as_str()
        .expect("a signed state")
        .to_string();
    let answer = json!({"user_name": {"action": "accept", "content": {"name": "Alice"}}});
    // the retry: same request, the answers, the state echoed — the tool sees its own state back, verified
    let retry = ask(json!({"inputResponses": answer, "requestState": state}));
    let r = modern_as(&n, "k1", elicit.clone(), "tools/call", retry.clone(), &[]).await;
    assert_eq!(
        tool_text(&r.json()),
        r#"Hello, Alice! state={"round": 1}"#,
        "{}",
        r.body
    );
    assert_eq!(r.json()["result"]["resultType"], "complete");
    // not single-use
    assert!(
        tool_text(
            &modern_as(&n, "k1", elicit.clone(), "tools/call", retry.clone(), &[])
                .await
                .json()
        )
        .starts_with("Hello")
    );
    // tampered, garbage, not a string: one answer, logged `state_invalid`
    for bad in [
        json!(format!("{state}-TAMPERED")),
        json!("garbage"),
        json!(12),
        json!(null),
    ] {
        let mut p = retry.clone();
        p["requestState"] = bad.clone();
        let r = modern_as(&n, "k1", elicit.clone(), "tools/call", p, &[]).await;
        assert_eq!(r.status, 200);
        assert_eq!(
            r.json()["error"],
            json!({"code": -32602, "message": "requestState integrity check failed"}),
            "{bad}"
        );
    }
    let line = log_line(|v| v["reason"] == "state_invalid").expect("a refused line");
    assert_eq!(
        (line["status"].as_str(), line["name"].as_str()),
        (Some("denied"), Some("ask"))
    );
    // another caller, other arguments, another method/name: refused
    let r = modern_as(&n, "k2", elicit.clone(), "tools/call", retry.clone(), &[]).await;
    assert_eq!(
        r.code(),
        Some(-32602),
        "another key must not ride the state"
    );
    let mut other_args = retry.clone();
    other_args["arguments"] = json!({});
    assert_eq!(
        modern_as(&n, "k1", elicit.clone(), "tools/call", other_args, &[])
            .await
            .code(),
        Some(-32602)
    );
    let mut other_tool = retry.clone();
    other_tool["name"] = json!("echo");
    other_tool["arguments"] = json!({"q": "state"});
    assert_eq!(
        modern_as(&n, "k1", elicit.clone(), "tools/call", other_tool, &[])
            .await
            .code(),
        Some(-32602)
    );
    // a tool that gave no state gets no requestState, and its retry needs none
    let r = modern_as(
        &n,
        "k1",
        elicit.clone(),
        "tools/call",
        json!({"name": "ask", "arguments": {"mode": "plain"}}),
        &[],
    )
    .await;
    assert!(
        r.json()["result"].get("requestState").is_none(),
        "{}",
        r.body
    );
    let r = modern_as(&n, "k1", elicit.clone(), "tools/call",
        json!({"name": "ask", "arguments": {"mode": "plain"}, "inputResponses": {"user_name": {"action": "accept", "content": {"name": "Bo"}}, "extra": {"x": 1}}}), &[]).await;
    assert_eq!(tool_text(&r.json()), "Hello, Bo! state=null");
    // a wrong key in the answers is the tool's to judge: it asks again
    let r = modern_as(&n, "k1", elicit.clone(), "tools/call",
        json!({"name": "ask", "arguments": {"mode": "plain"}, "inputResponses": {"wrong_key": {"action": "accept"}}}), &[]).await;
    assert_eq!(r.json()["result"]["resultType"], "input_required");
    // inputResponses must be an object of objects
    for bad in [
        json!(null),
        json!({"user_name": 12345}),
        json!([1]),
        json!("x"),
    ] {
        let r = modern_as(
            &n,
            "k1",
            elicit.clone(),
            "tools/call",
            json!({"name": "ask", "arguments": {}, "inputResponses": bad}),
            &[],
        )
        .await;
        assert_eq!((r.status, r.code()), (200, Some(-32602)), "{bad}");
    }
    // an ask the client did not declare, and a tool that says it cannot run: -32021, 400
    let r = modern_as(&n, "k1", json!({}), "tools/call", ask(json!({})), &[]).await;
    assert_eq!((r.status, r.code()), (400, Some(-32021)), "{}", r.body);
    assert_eq!(
        r.json()["error"]["data"],
        json!({"requiredCapabilities": {"elicitation": {}}})
    );
    let r = modern_as(
        &n,
        "k1",
        elicit.clone(),
        "tools/call",
        json!({"name": "ask", "arguments": {"mode": "url"}}),
        &[],
    )
    .await;
    assert_eq!(
        r.json()["error"]["data"],
        json!({"requiredCapabilities": {"elicitation": {"url": {}}}})
    );
    let both = json!({"elicitation": {"form": {}, "url": {}}});
    let r = modern_as(
        &n,
        "k1",
        both,
        "tools/call",
        json!({"name": "ask", "arguments": {"mode": "url"}}),
        &[],
    )
    .await;
    assert_eq!(
        r.json()["result"]["inputRequests"]["go"]["params"]["mode"],
        "url"
    );
    let r = modern_as(
        &n,
        "k1",
        elicit.clone(),
        "tools/call",
        json!({"name": "ask", "arguments": {"mode": "sampling"}}),
        &[],
    )
    .await;
    assert_eq!(
        r.json()["error"]["data"],
        json!({"requiredCapabilities": {"sampling": {}}})
    );
    let r = modern(
        &n,
        "tools/call",
        json!({"name": "needs_sampling", "arguments": {}}),
    )
    .await;
    assert_eq!(
        (r.status, r.code(), r.json()["id"].as_i64()),
        (400, Some(-32021), Some(7))
    );
    assert_eq!(
        r.json()["error"]["data"],
        json!({"requiredCapabilities": {"sampling": {}}})
    );
    let r = modern_as(
        &n,
        "k1",
        json!({"sampling": {}}),
        "tools/call",
        json!({"name": "needs_sampling", "arguments": {}}),
        &[],
    )
    .await;
    assert_eq!(tool_text(&r.json()), "Success");
    // the runtime is told what the client declared, and nothing of the transport
    let r = modern_as(
        &n,
        "k1",
        json!({"roots": {}}),
        "tools/call",
        json!({"name": "echo", "arguments": {"q": "x"}}),
        &[(
            "traceparent",
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        )],
    )
    .await;
    let seen: Value = serde_json::from_str(&tool_text(&r.json())).unwrap();
    assert_eq!(
        seen,
        json!({"arguments": {"q": "x"}, "client": {"capabilities": {"roots": {}}}, "progress": false, "inputResponses": null, "state": null})
    );
    // prompts ask the same way
    let r = modern_as(
        &n,
        "k1",
        elicit.clone(),
        "prompts/get",
        json!({"name": "ask_prompt"}),
        &[],
    )
    .await;
    assert_eq!(
        r.json()["result"]["inputRequests"]["user_context"]["method"],
        "elicitation/create"
    );
    let r = modern_as(&n, "k1", elicit.clone(), "prompts/get",
        json!({"name": "ask_prompt", "inputResponses": {"user_context": {"action": "accept", "content": {"context": "c1"}}}}), &[]).await;
    assert_eq!(
        r.json()["result"]["messages"][0]["content"]["text"],
        "context: c1"
    );
    // legacy: the ask becomes a tool error (or -32602 for a prompt) that says why
    let text = "this tool needs more input (user_name); it can only ask a client that speaks MCP 2026-07-28";
    let r = legacy(&n, "tools/call", json!({"name": "ask", "arguments": {}})).await;
    assert_eq!(
        r.json()["result"],
        json!({"content": [{"type": "text", "text": text}], "isError": true})
    );
    // ...and a legacy request's inputResponses / requestState are not a retry
    let r = legacy(
        &n,
        "tools/call",
        json!({"name": "ask", "arguments": {}, "inputResponses": answer, "requestState": "x"}),
    )
    .await;
    assert_eq!(r.json()["result"]["isError"], true);
    let r = legacy(&n, "prompts/get", json!({"name": "ask_prompt"})).await;
    assert_eq!(
        r.json()["error"],
        json!({"code": -32602, "message": text.replace("user_name", "user_context")})
    );
    let r = legacy(
        &n,
        "tools/call",
        json!({"name": "needs_sampling", "arguments": {}}),
    )
    .await;
    assert_eq!(
        tool_text(&r.json()),
        "this tool needs a client capability (sampling); it can only run for a client that speaks MCP 2026-07-28"
    );
    assert_eq!(r.json()["result"]["isError"], true);
}

/// §23.4: with a progressToken and an SSE-capable client the POST is a stream — progress first, then the response.
#[tokio::test]
async fn progress_is_streamed_when_asked_for_and_dropped_otherwise() {
    let n = node(&[]).await;
    let call =
        json!({"name": "prog", "arguments": {}, "_meta": {"progressToken": "progress-test-1"}});
    let r = modern_as(&n, "k1", json!({}), "tools/call", call.clone(), &[SSE]).await;
    assert_eq!(
        (r.status, r.header("content-type")),
        (200, Some("text/event-stream")),
        "{}",
        r.body
    );
    assert_eq!(r.header("x-accel-buffering"), Some("no"));
    let ev = r.events();
    assert_eq!(ev.len(), 4, "{}", r.body);
    for (e, want) in ev.iter().zip([0, 50, 100]) {
        assert_eq!(e["method"], "notifications/progress");
        assert_eq!(e["params"]["progressToken"], "progress-test-1");
        assert_eq!(
            (
                e["params"]["progress"].as_i64(),
                e["params"]["total"].as_i64()
            ),
            (Some(want), Some(100))
        );
        assert!(e.get("id").is_none());
    }
    assert_eq!(ev[1]["params"]["message"], "half");
    assert!(ev[0]["params"].get("message").is_none() && ev[0]["params"].get("id").is_none());
    assert_eq!(
        (ev[3]["id"].as_i64(), ev[3]["result"]["resultType"].as_str()),
        (Some(7), Some("complete"))
    );
    assert_eq!(tool_text(&ev[3]), "done progress=true");
    // JSON-only client, or no token: one JSON answer, the lines dropped, the tool told not to bother
    let r = modern_as(&n, "k1", json!({}), "tools/call", call.clone(), &[]).await;
    assert!(
        r.header("content-type")
            .unwrap()
            .starts_with("application/json")
    );
    assert_eq!(tool_text(&r.json()), "done progress=false");
    let r = modern_as(
        &n,
        "k1",
        json!({}),
        "tools/call",
        json!({"name": "prog", "arguments": {}}),
        &[SSE],
    )
    .await;
    assert!(
        r.header("content-type")
            .unwrap()
            .starts_with("application/json")
    );
    // asked for, nothing reported: still a stream, of one event
    let r = modern_as(
        &n,
        "k1",
        json!({}),
        "tools/call",
        json!({"name": "echo", "arguments": {}, "_meta": {"progressToken": 5}}),
        &[SSE],
    )
    .await;
    assert_eq!(r.header("content-type"), Some("text/event-stream"));
    assert_eq!(r.events().len(), 1);
    assert!(tool_text(&r.events()[0]).contains(r#""progress": true"#));
    // a refusal keeps its status even when a stream was asked for
    let r = modern_as(
        &n,
        "k1",
        json!({}),
        "tools/call",
        call.clone(),
        &[SSE, ("Mcp-Name", "other")],
    )
    .await;
    assert_eq!((r.status, r.code()), (400, Some(-32020)));
    // either era: a 2025-06-18 client with a token gets the same stream, and its own envelope
    let body = json!({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": call});
    let r = post(
        n.addr,
        &[("Authorization", "Bearer k1"), SSE],
        &body.to_string(),
    )
    .await;
    assert_eq!(r.header("content-type"), Some("text/event-stream"));
    assert_eq!(r.header("mcp-protocol-version"), Some("2025-06-18"));
    let ev = r.events();
    assert_eq!(ev.len(), 4);
    assert_eq!(
        ev[3]["result"],
        json!({"content": [{"type": "text", "text": "done progress=true"}], "isError": false})
    );
    // gRPC has no stream: dropped
    let r = grpc(
        &n,
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call}),
    )
    .await;
    assert_eq!(tool_text(&r), "done progress=false");
}

/// §23.1 `completion/complete` on both eras, and resource templates.
#[tokio::test]
async fn completion_reads_declared_enums_on_both_eras() {
    let n = node(&[]).await;
    let ask = |kind: &str, key: &str, target: &str, arg: &str, value: &str| json!({"ref": {"type": kind, key: target}, "argument": {"name": arg, "value": value}});
    let r = modern(
        &n,
        "completion/complete",
        ask("ref/prompt", "name", "greet", "tone", "w"),
    )
    .await;
    assert_eq!(
        r.json()["result"]["completion"],
        json!({"values": ["warm", "wry"], "total": 2, "hasMore": false}),
        "{}",
        r.body
    );
    assert_eq!(r.json()["result"]["resultType"], "complete");
    let r = legacy(
        &n,
        "completion/complete",
        ask("ref/prompt", "name", "greet", "tone", ""),
    )
    .await;
    assert_eq!(
        r.json()["result"],
        json!({"completion": {"values": ["warm", "wry", "cold"], "total": 3, "hasMore": false}})
    );
    let r = modern(
        &n,
        "completion/complete",
        ask("ref/resource", "uri", "ramen://items/{id}/data", "id", "al"),
    )
    .await;
    assert_eq!(
        r.json()["result"]["completion"]["values"],
        json!(["alpha", "alps"])
    );
    // nothing declared, unknown argument, unknown prompt: no completions, not an error
    for p in [
        ask("ref/prompt", "name", "greet", "who", ""),
        ask("ref/prompt", "name", "greet", "nope", ""),
        ask("ref/prompt", "name", "nope", "tone", ""),
        ask(
            "ref/prompt",
            "name",
            "test_prompt_with_arguments",
            "arg1",
            "test",
        ),
    ] {
        let r = modern(&n, "completion/complete", p).await;
        assert_eq!(
            r.json()["result"]["completion"],
            json!({"values": [], "total": 0, "hasMore": false}),
            "{}",
            r.body
        );
    }
    let r = modern(
        &n,
        "completion/complete",
        json!({"argument": {"name": "x"}}),
    )
    .await;
    assert_eq!((r.status, r.code()), (200, Some(-32602)));
    let r = modern(
        &n,
        "completion/complete",
        ask("ref/other", "name", "greet", "tone", ""),
    )
    .await;
    assert_eq!(r.code(), Some(-32602));
    // templates: listed apart from resources, on both eras
    let t = legacy(&n, "resources/templates/list", json!({}))
        .await
        .json()["result"]
        .clone();
    assert_eq!(t["resourceTemplates"].as_array().unwrap().len(), 2);
    assert!(t.get("ttlMs").is_none());
    assert_eq!(
        legacy(&n, "resources/list", json!({})).await.json()["result"]["resources"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}

async fn grpc(n: &Node, body: Value) -> Value {
    let ch = tonic::transport::Endpoint::from_shared(format!("http://{}", n.addr))
        .unwrap()
        .connect()
        .await
        .unwrap();
    let mut req = tonic::Request::new(JsonRpc {
        body: body.to_string().into_bytes(),
    });
    req.metadata_mut()
        .insert("authorization", "Bearer k1".parse().unwrap());
    let out = McpClient::new(ch)
        .call(req)
        .await
        .unwrap()
        .into_inner()
        .body;
    serde_json::from_slice(&out).unwrap_or(Value::Null)
}

/// §23.1: over gRPC the body alone selects the era, and the header rules do not apply.
#[tokio::test]
async fn grpc_serves_the_modern_era_by_the_body_alone() {
    let n = node(&[]).await;
    log_path();
    let m = |method: &str, mut params: Value, caps: Value| {
        params["_meta"] = meta(caps);
        json!({"jsonrpc": "2.0", "id": 11, "method": method, "params": params})
    };
    let r = grpc(&n, m("tools/list", json!({}), json!({}))).await;
    assert_eq!(
        (
            r["result"]["resultType"].as_str(),
            r["result"]["cacheScope"].as_str()
        ),
        (Some("complete"), Some("private")),
        "{r}"
    );
    let r = grpc(&n, m("server/discover", json!({}), json!({}))).await;
    assert_eq!(r["result"]["supportedVersions"][0], V);
    // no Mcp-Name, no Mcp-Param-*: fine here
    let r = grpc(
        &n,
        m(
            "tools/call",
            json!({"name": "echo", "arguments": {"region": "us"}}),
            json!({}),
        ),
    )
    .await;
    assert_eq!(r["result"]["resultType"], "complete", "{r}");
    assert_eq!(
        grpc(&n, m("ping", json!({}), json!({}))).await["error"]["code"],
        -32601
    );
    assert_eq!(
        grpc(
            &n,
            m(
                "subscriptions/listen",
                json!({"notifications": {}}),
                json!({})
            )
        )
        .await["error"]["code"],
        -32601
    );
    assert_eq!(
        grpc(&n, m("tools/call", json!({"name": "nope"}), json!({}))).await["error"]["message"],
        "Unknown tool: nope"
    );
    let mut old = m("tools/list", json!({}), json!({}));
    old["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] = json!("2025-06-18");
    let r = grpc(&n, old).await;
    assert_eq!(
        (r["error"]["code"].as_i64(), r["id"].as_i64()),
        (Some(-32022), Some(11))
    );
    let r = grpc(&n, json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": V}}})).await;
    assert_eq!(r["error"]["code"], -32602);
    // MRTR and tasks work the same, bound to the gRPC caller
    let elicit = json!({"elicitation": {}});
    let r = grpc(
        &n,
        m(
            "tools/call",
            json!({"name": "ask", "arguments": {}}),
            elicit.clone(),
        ),
    )
    .await;
    let state = r["result"]["requestState"].as_str().unwrap().to_string();
    let r = grpc(
        &n,
        m(
            "tools/call",
            json!({"name": "ask", "arguments": {}, "requestState": state,
        "inputResponses": {"user_name": {"action": "accept", "content": {"name": "G"}}}}),
            elicit,
        ),
    )
    .await;
    assert!(tool_text(&r).starts_with("Hello, G!"), "{r}");
    // a bare request stays legacy, exactly as before
    let r = grpc(
        &n,
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
    )
    .await;
    assert!(r["result"].get("resultType").is_none());
    assert_eq!(
        grpc(&n, json!({"jsonrpc": "2.0", "id": 1, "method": "ping"})).await["result"],
        json!({})
    );
    // the access log and the metrics name the protocol served
    let line = log_line(|v| v["transport"] == "grpc" && v["method"] == "server/discover").unwrap();
    assert_eq!(line["protocol"], V);
    let line =
        log_line(|v| v["transport"] == "grpc" && v["method"] == "ping" && v["status"] == "ok")
            .unwrap();
    assert_eq!(line["protocol"], "2025-06-18");
    let metrics = n.app.metrics_json().await;
    assert!(
        metrics["calls_by_protocol"][V].as_u64().unwrap() >= 5,
        "{metrics}"
    );
    assert!(metrics["calls_by_protocol"]["2025-06-18"].as_u64().unwrap() >= 2);
    assert_eq!(
        (metrics["subscriptions"].as_u64(), metrics["tasks"].as_u64()),
        (Some(0), Some(0))
    );
}

fn tasks_caps() -> Value {
    json!({"extensions": {TASKS: {}}, "elicitation": {}})
}

async fn task_get(n: &Node, key: &str, id: &str) -> Value {
    modern_as(
        n,
        key,
        tasks_caps(),
        "tasks/get",
        json!({"taskId": id}),
        &[],
    )
    .await
    .json()
}

/// Polls `tasks/get` until the task reaches `status` (5 s).
async fn task_until(n: &Node, id: &str, status: &str) -> Value {
    let mut last = Value::Null;
    for _ in 0..100 {
        last = task_get(n, "k1", id).await;
        if last["result"]["status"] == status {
            return last["result"].clone();
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("task {id} never reached {status}: {last}");
}

async fn start_task(n: &Node, mode: &str) -> Value {
    let r = modern_as(
        n,
        "k1",
        tasks_caps(),
        "tools/call",
        json!({"name": "slow", "arguments": {"mode": mode}}),
        &[],
    )
    .await;
    assert_eq!(r.status, 200, "{}", r.body);
    r.json()["result"].clone()
}

/// §23.5: a task tool called by a declaring client becomes a task the caller — and only the caller — can follow.
#[tokio::test]
async fn tasks_run_are_bound_to_the_caller_and_end_in_every_way() {
    let n = node(&[("RAMEN_TASK_TTL_SECS", "120")]).await;
    log_path();
    let t = start_task(&n, "ok").await;
    assert_eq!(
        (t["resultType"].as_str(), t["status"].as_str()),
        (Some("task"), Some("working")),
        "{t}"
    );
    assert_eq!(
        (t["ttlMs"].as_u64(), t["pollIntervalMs"].as_u64()),
        (Some(120_000), Some(2000))
    );
    let id = t["taskId"].as_str().unwrap().to_string();
    assert!(
        id.len() == 32 && id.bytes().all(|b| b.is_ascii_hexdigit()),
        "{id}"
    );
    assert!(t["createdAt"].is_string() && t["lastUpdatedAt"].is_string());
    assert!(t.get("x").is_none() && t.get("requestState").is_none() && t.get("task").is_none());
    // readable at once, by the caller
    let got = task_get(&n, "k1", &id).await;
    assert_eq!(got["result"]["resultType"], "complete", "{got}");
    assert_eq!(got["result"]["taskId"], id.as_str());
    let done = task_until(&n, &id, "completed").await;
    assert_eq!(
        done["result"],
        json!({"content": [{"type": "text", "text": "task done"}], "isError": false})
    );
    assert!(done.get("x").is_none() && done.get("error").is_none());
    // another caller, an unknown id: the same -32602
    for (key, tid) in [
        ("k2", id.as_str()),
        ("k1", "00000000000000000000000000000000"),
    ] {
        let r = modern_as(
            &n,
            key,
            tasks_caps(),
            "tasks/get",
            json!({"taskId": tid}),
            &[],
        )
        .await;
        assert_eq!(r.status, 200);
        assert_eq!(
            r.json()["error"],
            json!({"code": -32602, "message": "Unknown task"})
        );
    }
    for m in ["tasks/update", "tasks/cancel"] {
        let r = modern_as(
            &n,
            "k2",
            tasks_caps(),
            m,
            json!({"taskId": id, "inputResponses": {}}),
            &[],
        )
        .await;
        assert_eq!(r.code(), Some(-32602), "{m}");
    }
    // without the extension declared: the task methods are -32021 and the tool runs synchronously
    for m in ["tasks/get", "tasks/update", "tasks/cancel"] {
        let r = modern_as(&n, "k1", json!({}), m, json!({"taskId": id}), &[]).await;
        assert_eq!((r.status, r.code()), (400, Some(-32021)), "{m}");
        assert_eq!(
            r.json()["error"]["data"],
            json!({"requiredCapabilities": {"extensions": {TASKS: {}}}})
        );
    }
    let r = modern(
        &n,
        "tools/call",
        json!({"name": "slow", "arguments": {"mode": "ok"}}),
    )
    .await;
    assert_eq!(
        (
            tool_text(&r.json()),
            r.json()["result"]["resultType"].as_str()
        ),
        ("sync:ok".to_string(), Some("complete"))
    );
    assert_eq!(
        tool_text(
            &legacy(&n, "tools/call", json!({"name": "slow", "arguments": {}}))
                .await
                .json()
        ),
        "sync:ok"
    );
    // Mcp-Name must be the task id
    let r = modern_as(
        &n,
        "k1",
        tasks_caps(),
        "tasks/get",
        json!({"taskId": id}),
        &[("Mcp-Name", "wrong")],
    )
    .await;
    assert_eq!((r.status, r.code()), (400, Some(-32020)));
    // a tool error is a completed task; a protocol failure is a failed one with the error inlined
    let failed = start_task(&n, "fail").await;
    let r = task_until(&n, failed["taskId"].as_str().unwrap(), "completed").await;
    assert_eq!(r["result"]["isError"], true);
    let broken = start_task(&n, "error").await;
    let r = task_until(&n, broken["taskId"].as_str().unwrap(), "failed").await;
    assert_eq!(
        r["error"],
        json!({"code": -32603, "message": "protocol trouble"})
    );
    assert!(r.get("result").is_none());
    // input: the task parks, tasks/update moves it on
    let parked = start_task(&n, "input").await;
    let pid = parked["taskId"].as_str().unwrap().to_string();
    let r = task_until(&n, &pid, "input_required").await;
    assert_eq!(
        r["inputRequests"]["confirm"]["method"],
        "elicitation/create"
    );
    let r = modern_as(
        &n,
        "k1",
        tasks_caps(),
        "tasks/update",
        json!({"taskId": pid, "inputResponses": {"confirm": 5}}),
        &[],
    )
    .await;
    assert_eq!(r.code(), Some(-32602));
    let r = modern_as(&n, "k1", tasks_caps(), "tasks/update",
        json!({"taskId": pid, "inputResponses": {"confirm": {"action": "accept", "content": {"ok": true}}}}), &[]).await;
    let ack = r.json()["result"].clone();
    assert_eq!(ack["resultType"], "complete", "{}", r.body);
    assert!(ack.get("taskId").is_none());
    let r = task_until(&n, &pid, "completed").await;
    assert_eq!(r["result"]["content"][0]["text"], "confirmed");
    assert!(r.get("inputRequests").is_none());
    // cancel: a running task ends cancelled; cancelling a finished one is still an ack
    let long = start_task(&n, "long").await;
    let lid = long["taskId"].as_str().unwrap().to_string();
    assert_eq!(n.app.metrics_json().await["tasks"], 1);
    let r = modern_as(
        &n,
        "k1",
        tasks_caps(),
        "tasks/cancel",
        json!({"taskId": lid}),
        &[],
    )
    .await;
    assert_eq!(r.json()["result"]["resultType"], "complete");
    let r = task_until(&n, &lid, "cancelled").await;
    assert!(r.get("result").is_none() && r.get("error").is_none());
    let r = modern_as(
        &n,
        "k1",
        tasks_caps(),
        "tasks/cancel",
        json!({"taskId": lid}),
        &[],
    )
    .await;
    assert_eq!(r.json()["result"]["resultType"], "complete");
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(n.app.metrics_json().await["tasks"], 0);
    // the access log names the tool and the task on tasks/* lines, and the task on the call that made it
    let line = log_line(|v| v["method"] == "tasks/cancel" && v["task"] == lid.as_str())
        .expect("a tasks/cancel line");
    assert_eq!(
        (line["name"].as_str(), line["status"].as_str()),
        (Some("slow"), Some("ok"))
    );
    let line = log_line(|v| v["method"] == "tools/call" && v["task"] == lid.as_str())
        .expect("the creating line");
    assert_eq!(line["name"], "slow");
    // discover advertises the extension because a task tool is loaded
    let d = modern(&n, "server/discover", json!({})).await.json()["result"]["capabilities"].clone();
    assert_eq!(d["extensions"], json!({TASKS: {}}));
}

/// §23.5: `RAMEN_MAX_TASKS` running per pod, then 429; the TTL ends a task; the sidecar outlives its idle limit
/// while a task runs.
#[tokio::test]
async fn task_limits_ttl_and_the_idle_reaper() {
    let n = node(&[
        ("RAMEN_MAX_TASKS", "1"),
        ("RAMEN_TASK_TTL_SECS", "4"),
        ("RAMEN_SIDECAR_IDLE_SECS", "1"),
    ])
    .await;
    log_path();
    n.app.sidecar.start_reaper();
    // poll far slower than the idle limit: for 2.6 s nothing calls the sidecar, so only the hold keeps it (and
    // the task in it) alive — reaped, the poll would find a fresh runtime and report `worker lost`
    n.app.tasks.poll_ms.store(2600, Relaxed);
    let first = start_task(&n, "ok").await;
    let r = modern_as(
        &n,
        "k1",
        tasks_caps(),
        "tools/call",
        json!({"name": "slow", "arguments": {"mode": "long"}}),
        &[],
    )
    .await;
    assert_eq!(r.status, 429, "{}", r.body);
    assert_eq!(
        log_line(|v| v["reason"] == "max_tasks").unwrap()["status"],
        "denied"
    );
    // a caller that does not ask for a task is not counted against the limit
    assert_eq!(
        modern(&n, "tools/call", json!({"name": "slow", "arguments": {}}))
            .await
            .status,
        200
    );
    tokio::time::sleep(Duration::from_millis(2800)).await;
    let done = task_get(&n, "k1", first["taskId"].as_str().unwrap()).await["result"].clone();
    assert_eq!(
        done["status"], "completed",
        "the sidecar was reaped under a running task: {done}"
    );
    assert_eq!(done["result"]["content"][0]["text"], "task done");
    // past its TTL the task is unknown, and its slot is free again
    n.app.tasks.poll_ms.store(40, Relaxed);
    let long = start_task(&n, "long").await;
    tokio::time::sleep(Duration::from_millis(4600)).await;
    let r = modern_as(
        &n,
        "k1",
        tasks_caps(),
        "tasks/get",
        json!({"taskId": long["taskId"]}),
        &[],
    )
    .await;
    assert_eq!(r.code(), Some(-32602), "{}", r.body);
    assert_eq!(n.app.tasks.live(), 0);
}

/// An open `subscriptions/listen` stream: frames are read as they arrive.
struct Stream {
    sock: TcpStream,
    buf: String,
    seen: usize,
    status: u16,
    headers: Vec<(String, String)>,
}

impl Stream {
    async fn open(n: &Node, key: &str, id: Value, caps: Value, notifications: Value) -> Stream {
        let auth = bearer(key);
        let body = json!({"jsonrpc": "2.0", "id": id, "method": "subscriptions/listen",
                          "params": {"_meta": meta(caps), "notifications": notifications}});
        let hs = [
            ("Authorization", auth.as_str()),
            ("MCP-Protocol-Version", V),
            ("Mcp-Method", "subscriptions/listen"),
            SSE,
        ];
        let mut sock = TcpStream::connect(n.addr).await.unwrap();
        sock.write_all(request(n.addr, &hs, &body.to_string()).as_bytes())
            .await
            .unwrap();
        let mut s = Stream {
            sock,
            buf: String::new(),
            seen: 0,
            status: 0,
            headers: vec![],
        };
        while !s.buf.contains("\r\n\r\n") {
            assert!(s.read(2000).await, "response head in time");
        }
        let (h, _) = s.buf.split_once("\r\n\r\n").unwrap();
        (s.status, s.headers) = head(h);
        s
    }

    /// Reads once more; `false` on timeout or end of stream.
    async fn read(&mut self, ms: u64) -> bool {
        let mut chunk = [0u8; 4096];
        match tokio::time::timeout(Duration::from_millis(ms), self.sock.read(&mut chunk)).await {
            Ok(Ok(k)) if k > 0 => {
                self.buf.push_str(&String::from_utf8_lossy(&chunk[..k]));
                true
            }
            _ => false,
        }
    }

    /// The next JSON frame, or `None` when none arrives within `ms` (keep-alive comments are not frames).
    async fn next(&mut self, ms: u64) -> Option<Value> {
        let deadline = std::time::Instant::now() + Duration::from_millis(ms);
        loop {
            let body = self
                .buf
                .split_once("\r\n\r\n")
                .map(|(_, b)| b)
                .unwrap_or("");
            let ev = events_of(body);
            if ev.len() > self.seen {
                self.seen += 1;
                return Some(ev[self.seen - 1].clone());
            }
            let left = deadline.saturating_duration_since(std::time::Instant::now());
            if left.is_zero() || !self.read(left.as_millis() as u64).await {
                return None;
            }
        }
    }
}

/// §23.6: the ack first and at once; a reload that changes the manifest is announced per requested kind, tagged;
/// keep-alive comments; the cap; the completion result on drain.
#[tokio::test]
async fn subscriptions_listen_streams_list_changes_per_pod() {
    let n = node(&[("RAMEN_MAX_SUBSCRIPTIONS", "2")]).await;
    log_path();
    n.app.subs.keepalive_ms.store(150, Relaxed);
    let t0 = std::time::Instant::now();
    let mut a = Stream::open(&n, "k1", json!(4242), json!({}), json!({"toolsListChanged": true, "resourcesListChanged": true, "resourceSubscriptions": ["file:///x"]})).await;
    assert_eq!(a.status, 200, "{}", a.buf);
    let h = |s: &Stream, k: &str| {
        s.headers
            .iter()
            .find(|(n, _)| n.eq_ignore_ascii_case(k))
            .map(|(_, v)| v.clone())
    };
    assert_eq!(h(&a, "content-type").as_deref(), Some("text/event-stream"));
    assert_eq!(h(&a, "x-accel-buffering").as_deref(), Some("no"));
    assert!(h(&a, "mcp-session-id").is_none());
    let ack = a.next(1000).await.expect("the ack");
    assert!(
        t0.elapsed() < Duration::from_millis(600),
        "the ack is flushed at once"
    );
    assert_eq!(ack["method"], "notifications/subscriptions/acknowledged");
    assert_eq!(ack["params"]["_meta"][SUB], 4242);
    assert_eq!(
        ack["params"]["notifications"],
        json!({"toolsListChanged": true, "resourcesListChanged": true})
    );
    let mut b = Stream::open(
        &n,
        "k2",
        json!("sub-b"),
        json!({}),
        json!({"promptsListChanged": true}),
    )
    .await;
    assert_eq!(
        b.next(1000).await.unwrap()["params"]["notifications"],
        json!({"promptsListChanged": true})
    );
    assert_eq!(n.app.metrics_json().await["subscriptions"], 2);
    // the cap: a third stream is 429, and holds no inflight permit meanwhile
    let c = Stream::open(&n, "k1", json!(3), json!({}), json!({})).await;
    assert_eq!(c.status, 429, "{}", c.buf);
    assert_eq!(
        log_line(|v| v["reason"] == "max_subscriptions").unwrap()["status"],
        "denied"
    );
    assert_eq!(n.app.metrics_json().await["inflight"], 0);
    // a reload with the same manifest says nothing
    n.app.load().await.unwrap();
    // ...one with a different hash is announced: one notification per requested kind, each tagged
    std::fs::write(n.bucket.join("hash"), "h2").unwrap();
    n.app.load().await.unwrap();
    let (x, y) = (
        a.next(1000).await.expect("tools"),
        a.next(1000).await.expect("resources"),
    );
    assert_eq!(
        x,
        json!({"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {"_meta": {SUB: 4242}}})
    );
    assert_eq!(y["method"], "notifications/resources/list_changed");
    assert_eq!(y["params"]["_meta"][SUB], 4242);
    let p = b.next(1000).await.expect("prompts");
    assert_eq!(
        (p["method"].as_str(), p["params"]["_meta"][SUB].as_str()),
        (Some("notifications/prompts/list_changed"), Some("sub-b"))
    );
    // nothing else was sent to either (b did not ask for tools), but the keep-alive comment arrives
    assert!(a.next(400).await.is_none() && b.next(50).await.is_none());
    assert!(a.buf.contains(": keep-alive"), "{}", a.buf);
    assert_eq!(a.buf.matches("list_changed").count(), 2);
    // a client that goes away frees its slot
    drop(b);
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(n.app.metrics_json().await["subscriptions"], 1);
    // tasks: asking without the extension is -32021; with it, the caller's own task ids are watched
    let r = modern_as(
        &n,
        "k1",
        json!({}),
        "subscriptions/listen",
        json!({"notifications": {"taskIds": ["x"]}}),
        &[SSE],
    )
    .await;
    assert_eq!((r.status, r.code()), (400, Some(-32021)));
    let t = start_task(&n, "long").await;
    let tid = t["taskId"].as_str().unwrap().to_string();
    let mut w = Stream::open(
        &n,
        "k1",
        json!(77),
        tasks_caps(),
        json!({"taskIds": [tid, "not-mine"]}),
    )
    .await;
    assert_eq!(
        w.next(1000).await.unwrap()["params"]["notifications"],
        json!({"taskIds": [tid]})
    );
    let _ = modern_as(
        &n,
        "k1",
        tasks_caps(),
        "tasks/cancel",
        json!({"taskId": tid}),
        &[],
    )
    .await;
    let note = w.next(2000).await.expect("a task notification");
    assert_eq!(note["method"], "notifications/tasks");
    assert_eq!(
        (
            note["params"]["taskId"].as_str(),
            note["params"]["status"].as_str()
        ),
        (Some(tid.as_str()), Some("cancelled"))
    );
    assert_eq!(note["params"]["_meta"][SUB], 77);
    assert!(note["params"].get("x").is_none());
    // drain: every stream gets its completion result and ends
    n.app.subs.drain();
    let done = a.next(1000).await.expect("the completion result");
    assert_eq!(
        (done["id"].as_i64(), done["result"]["resultType"].as_str()),
        (Some(4242), Some("complete"))
    );
    assert_eq!(done["result"]["_meta"][SUB], 4242);
    while a.read(500).await {}
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(n.app.metrics_json().await["subscriptions"], 0);
    let line = log_line(|v| v["method"] == "subscriptions/listen" && v["status"] == "ok").unwrap();
    assert_eq!(
        (line["subscription"].as_str(), line["protocol"].as_str()),
        (Some("open"), Some(V))
    );
}

/// §23.1: the access log names the version served on every line, including the legacy ones.
#[tokio::test]
async fn the_access_log_names_the_protocol_on_both_eras() {
    let n = node(&[]).await;
    log_path();
    let body = r#"{"jsonrpc":"2.0","id":1,"method":"resources/templates/list"}"#;
    let r = post(
        n.addr,
        &[
            ("Authorization", "Bearer k1"),
            ("MCP-Protocol-Version", "2025-03-26"),
        ],
        body,
    )
    .await;
    assert_eq!(
        (r.status, r.header("mcp-protocol-version")),
        (200, Some("2025-03-26"))
    );
    let line = log_line(|v| v["method"] == "resources/templates/list" && v["transport"] == "http")
        .unwrap();
    assert_eq!(line["protocol"], "2025-03-26");
    assert!(line.get("trace").is_none());
    // an unknown header version is a modern request now: refused with a JSON-RPC error, after the credential
    let r = post(
        n.addr,
        &[
            ("Authorization", "Bearer k1"),
            ("MCP-Protocol-Version", "1999-01-01"),
        ],
        body,
    )
    .await;
    assert_eq!((r.status, r.code()), (400, Some(-32602)));
    let r = post(n.addr, &[("MCP-Protocol-Version", "1999-01-01")], body).await;
    assert_eq!(r.status, 401);
    let m = n.app.metrics_json().await;
    assert_eq!(m["calls_by_protocol"]["2025-03-26"], 1, "{m}");
}
