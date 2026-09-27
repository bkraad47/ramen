//! HTTP surface tests; the ones marked `py` spawn the real Python runtime against the demo fixture.
use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use ramen_node::config::Config;
use ramen_node::http::{Shared, app_with, router};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use tower::ServiceExt;

fn cfg(extra: &[(&str, &str)]) -> Config {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../runtime-py");
    let mut m: HashMap<String, String> = [
        ("RAMEN_MCP_KEYS", "k1"),
        ("RAMEN_ADMIN_KEY", "adm"),
        ("RAMEN_GROUP", "demo"),
        ("RAMEN_PYTHON", "python3"),
        ("RAMEN_CALL_TIMEOUT_SECS", "60"),
    ]
    .iter()
    .map(|(k, v)| (k.to_string(), v.to_string()))
    .collect();
    m.insert(
        "RAMEN_BUCKET".into(),
        root.join("tests/fixtures/demo").display().to_string(),
    );
    // prefer the runtime-py uv venv (has jsonschema); else RAMEN_TEST_PYTHON; else python3
    let venv = root.join(".venv/bin/python");
    if let Ok(p) = std::env::var("RAMEN_TEST_PYTHON") {
        m.insert("RAMEN_PYTHON".into(), p);
    } else if venv.is_file() {
        m.insert("RAMEN_PYTHON".into(), venv.display().to_string());
    }
    m.insert(
        "RAMEN_PYTHONPATH".into(),
        root.join("src").display().to_string(),
    );
    m.extend(extra.iter().map(|(k, v)| (k.to_string(), v.to_string())));
    Config::from_map(&m).unwrap()
}

fn app(c: Config) -> Shared {
    let mut reloaded = c.clone();
    reloaded.mcp_keys.push("k2".into());
    app_with(c, Box::new(move || Ok(reloaded.clone())))
}

async fn send(
    state: &Shared,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: Option<Value>,
    peer: &str,
) -> (StatusCode, Value) {
    let mut req = Request::builder().method(method).uri(path);
    for (k, v) in headers {
        req = req.header(*k, *v);
    }
    let mut req = req
        .body(body.map_or(Body::empty(), |b| match b {
            Value::String(raw) => Body::from(raw),
            v => Body::from(v.to_string()),
        }))
        .unwrap();
    req.extensions_mut()
        .insert(ConnectInfo(peer.parse::<SocketAddr>().unwrap()));
    let resp = router(state.clone()).oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        serde_json::from_slice(&bytes)
            .unwrap_or(Value::String(String::from_utf8_lossy(&bytes).into())),
    )
}

async fn rpc(state: &Shared, key: &str, method: &str, params: Value) -> (StatusCode, Value) {
    let body = json!({"jsonrpc": "2.0", "id": 1, "method": method, "params": params});
    send(
        state,
        "POST",
        "/mcp",
        &[
            ("authorization", &format!("Bearer {key}")),
            ("content-type", "application/json"),
        ],
        Some(body),
        "127.0.0.1:9",
    )
    .await
}

fn python_ok() -> bool {
    let c = cfg(&[]);
    let ok = std::process::Command::new(&c.python)
        .env("PYTHONPATH", c.pythonpath.unwrap())
        .args(["-c", "import ramen_runtime, jsonschema"])
        .status()
        .is_ok_and(|s| s.success());
    if !ok {
        eprintln!("SKIP: python3 with ramen_runtime+jsonschema not available");
    }
    ok
}

#[tokio::test]
async fn health_auth_and_protocol_without_sidecar() {
    let a = app(cfg(&[("RAMEN_ALLOWED_CIDRS", "127.0.0.0/8")]));
    assert_eq!(
        send(&a, "GET", "/healthz", &[], None, "127.0.0.1:1")
            .await
            .0,
        StatusCode::OK
    );
    assert_eq!(
        send(&a, "GET", "/readyz", &[], None, "127.0.0.1:1").await.0,
        StatusCode::SERVICE_UNAVAILABLE
    );
    let (s, m) = send(&a, "GET", "/metrics", &[], None, "127.0.0.1:1").await;
    assert_eq!(
        (
            s,
            m["load"].as_str(),
            m["sidecar_alive"].as_bool(),
            m["inflight"].as_u64()
        ),
        (StatusCode::OK, Some("low"), Some(false), Some(0))
    );
    let (s, b) = send(
        &a,
        "POST",
        "/mcp",
        &[],
        Some(json!({"jsonrpc": "2.0", "id": 1, "method": "ping"})),
        "127.0.0.1:1",
    )
    .await;
    assert_eq!(
        (s, b["error"]["code"].as_i64()),
        (StatusCode::UNAUTHORIZED, Some(-32001))
    );
    let (s, _) = send(
        &a,
        "POST",
        "/mcp",
        &[("authorization", "Bearer k1")],
        Some(json!({"method": "ping"})),
        "10.0.0.1:1",
    )
    .await;
    assert_eq!(s, StatusCode::FORBIDDEN);
    let (s, b) = send(
        &a,
        "POST",
        "/mcp",
        &[("authorization", "Bearer k1")],
        Some(Value::String("{nope".into())),
        "127.0.0.1:1",
    )
    .await;
    assert_eq!(
        (s, b["error"]["code"].as_i64()),
        (StatusCode::BAD_REQUEST, Some(-32700))
    );
    let (s, b) = rpc(&a, "k1", "", json!({})).await;
    assert_eq!(
        (s, b["error"]["code"].as_i64()),
        (StatusCode::BAD_REQUEST, Some(-32600))
    );
    let (s, b) = rpc(&a, "k1", "initialize", json!({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})).await;
    assert_eq!(
        (
            s,
            b["result"]["protocolVersion"].as_str(),
            b["result"]["serverInfo"]["name"].as_str()
        ),
        (StatusCode::OK, Some("2025-06-18"), Some("ramen-node"))
    );
    assert_eq!(
        rpc(&a, "k1", "ping", json!({})).await.1["result"],
        json!({})
    );
    let (s, b) = send(
        &a,
        "POST",
        "/mcp",
        &[("authorization", "Bearer k1")],
        Some(json!({"jsonrpc": "2.0", "method": "notifications/initialized"})),
        "127.0.0.1:1",
    )
    .await;
    assert_eq!((s, b.as_str()), (StatusCode::ACCEPTED, Some("")));
    assert_eq!(
        rpc(&a, "k1", "tools/list", json!({})).await.1["error"]["code"],
        -32002
    );
    assert_eq!(
        rpc(&a, "k1", "nope/x", json!({})).await.1["error"]["code"],
        -32601
    );
    assert_eq!(
        rpc(&a, "k1", "tools/call", json!({"name": 1})).await.1["error"]["code"],
        -32602
    );
    assert_eq!(
        rpc(&a, "k1", "resources/read", json!({})).await.1["error"]["code"],
        -32602
    );
    assert_eq!(
        send(&a, "POST", "/admin/reload", &[], None, "127.0.0.1:1")
            .await
            .0,
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        send(
            &a,
            "POST",
            "/admin/reload",
            &[("x-ramen-admin-key", "wrong")],
            None,
            "127.0.0.1:1"
        )
        .await
        .0,
        StatusCode::FORBIDDEN
    );
    let permit = a
        .sem
        .acquire_many(a.cfg.read().await.max_inflight as u32)
        .await
        .unwrap();
    assert_eq!(
        rpc(&a, "k1", "ping", json!({})).await.0,
        StatusCode::SERVICE_UNAVAILABLE
    );
    drop(permit);
    let m = send(&a, "GET", "/metrics", &[], None, "127.0.0.1:1")
        .await
        .1;
    assert!(m["total"].as_u64().unwrap() >= 6 && m["errors"].as_u64().unwrap() >= 4);
}

#[tokio::test]
async fn trust_proxy_uses_forwarded_ip() {
    let strict = app(cfg(&[("RAMEN_ALLOWED_CIDRS", "10.0.0.0/8")]));
    let xff = [
        ("authorization", "Bearer k1"),
        ("x-forwarded-for", "10.1.1.1"),
    ];
    let ping = json!({"jsonrpc": "2.0", "id": 1, "method": "ping"});
    assert_eq!(
        send(
            &strict,
            "POST",
            "/mcp",
            &xff,
            Some(ping.clone()),
            "127.0.0.1:1"
        )
        .await
        .0,
        StatusCode::FORBIDDEN
    );
    let trusting = app(cfg(&[
        ("RAMEN_ALLOWED_CIDRS", "10.0.0.0/8"),
        ("RAMEN_TRUST_PROXY", "1"),
    ]));
    assert_eq!(
        send(&trusting, "POST", "/mcp", &xff, Some(ping), "127.0.0.1:1")
            .await
            .0,
        StatusCode::OK
    );
}

#[tokio::test]
async fn py_spawn_missing_python_is_internal_error() {
    let a = app(cfg(&[("RAMEN_PYTHON", "/nonexistent/python")]));
    let (s, b) = rpc(&a, "k1", "tools/call", json!({"name": "x"})).await;
    assert_eq!(
        (s, b["error"]["code"].as_i64()),
        (StatusCode::OK, Some(-32603))
    );
    assert!(b["error"]["message"].as_str().unwrap().contains("spawn"));
}

#[tokio::test]
async fn py_end_to_end_with_demo_repo() {
    if !python_ok() {
        return;
    }
    let a = app(cfg(&[("RAMEN_VERBOSE", "1")]));
    a.sidecar.load().await.unwrap();
    assert_eq!(
        send(&a, "GET", "/readyz", &[], None, "127.0.0.1:1").await.0,
        StatusCode::OK
    );
    let tools = rpc(&a, "k1", "tools/list", json!({})).await.1;
    assert_eq!(tools["result"]["tools"][0]["name"], "demo_calculator_tool");
    assert_eq!(
        tools["result"]["tools"][0]["inputSchema"]["required"],
        json!(["var1", "var2", "func"])
    );
    let r = rpc(
        &a,
        "k1",
        "tools/call",
        json!({"name": "demo_calculator_tool", "arguments": {"var1": 2, "var2": 3, "func": "add"}}),
    )
    .await
    .1;
    assert_eq!(
        r["result"],
        json!({"content": [{"type": "text", "text": "5"}], "isError": false})
    );
    let r = rpc(&a, "k1", "tools/call", json!({"name": "demo_calculator_tool", "arguments": {"var1": 2, "var2": 0, "func": "divide"}})).await.1;
    assert_eq!(r["result"]["isError"], true);
    assert_eq!(
        rpc(&a, "k1", "tools/call", json!({"name": "nope"})).await.1["error"]["code"],
        -32004
    );
    assert_eq!(
        rpc(&a, "k1", "resources/list", json!({})).await.1["result"]["resources"][0]["uri"],
        "ramen://demo/readme"
    );
    let r = rpc(
        &a,
        "k1",
        "resources/read",
        json!({"uri": "ramen://demo/readme"}),
    )
    .await
    .1;
    assert!(
        r["result"]["contents"][0]["text"]
            .as_str()
            .unwrap()
            .starts_with("# ramen-demo-mcp-group")
    );
    assert_eq!(
        rpc(&a, "k1", "prompts/list", json!({})).await.1["result"]["prompts"][0]["arguments"][0]["name"],
        "request"
    );
    let r = rpc(
        &a,
        "k1",
        "prompts/get",
        json!({"name": "get_calculation_prompt", "arguments": {"request": "9*9"}}),
    )
    .await
    .1;
    assert!(
        r["result"]["messages"][0]["content"]["text"]
            .as_str()
            .unwrap()
            .contains("Request: 9*9")
    );
    assert_eq!(
        rpc(
            &a,
            "k1",
            "prompts/get",
            json!({"name": "get_calculation_prompt"})
        )
        .await
        .1["error"]["code"],
        -32602
    );
    let m = send(&a, "GET", "/metrics", &[], None, "127.0.0.1:1")
        .await
        .1;
    assert_eq!(
        (
            m["sidecar_alive"].as_bool(),
            m["packages"]["tools"].as_u64(),
            m["packages"]["errors"].as_u64()
        ),
        (Some(true), Some(1), Some(0))
    );
    assert!(m["loaded_at"].is_string());
    // respawn after a kill re-runs runtime.load transparently
    a.sidecar.kill("test").await;
    assert!(!a.sidecar.alive().await);
    let r = rpc(&a, "k1", "tools/call", json!({"name": "demo_calculator_tool", "arguments": {"var1": 6, "var2": 7, "func": "multiply"}})).await.1;
    assert_eq!(r["result"]["content"][0]["text"], "42");
    // admin reload: re-reads config (k2 becomes valid) and reloads packages
    assert_eq!(
        rpc(&a, "k2", "ping", json!({})).await.0,
        StatusCode::UNAUTHORIZED
    );
    let (s, b) = send(
        &a,
        "POST",
        "/admin/reload",
        &[("x-ramen-admin-key", "adm")],
        None,
        "127.0.0.1:1",
    )
    .await;
    assert_eq!(
        (s, b["tools"][0]["name"].as_str()),
        (StatusCode::OK, Some("demo_calculator_tool"))
    );
    assert_eq!(rpc(&a, "k2", "ping", json!({})).await.0, StatusCode::OK);
}

#[tokio::test]
async fn py_idle_reaper_kills_and_call_respawns() {
    if !python_ok() {
        return;
    }
    let a = app(cfg(&[("RAMEN_SIDECAR_IDLE_SECS", "1")]));
    a.sidecar.start_reaper();
    a.sidecar.load().await.unwrap();
    assert!(a.sidecar.alive().await);
    for _ in 0..40 {
        tokio::time::sleep(std::time::Duration::from_millis(250)).await;
        if !a.sidecar.alive().await {
            break;
        }
    }
    assert!(
        !a.sidecar.alive().await,
        "reaper should have killed the idle sidecar"
    );
    let r = rpc(
        &a,
        "k1",
        "tools/call",
        json!({"name": "demo_calculator_tool", "arguments": {"var1": 1, "var2": 1, "func": "add"}}),
    )
    .await
    .1;
    assert_eq!(r["result"]["content"][0]["text"], "2");
}

#[tokio::test]
async fn py_call_timeout_kills_sidecar() {
    if !python_ok() {
        return;
    }
    let dir = std::env::temp_dir().join(format!("ramen-slow-{}", std::process::id()));
    let pkg = dir.join("mcp/tools/slow");
    std::fs::create_dir_all(&pkg).unwrap();
    std::fs::write(pkg.join("slow.json"), r#"{"type":"tool","name":"slow","description":"","callable":"f","input":{},"output":{"type":"string"},"error":{"type":"string"}}"#).unwrap();
    std::fs::write(
        pkg.join("slow.py"),
        "import time\ndef f():\n    time.sleep(5)\n    return 'late'\n",
    )
    .unwrap();
    let a = app(cfg(&[
        ("RAMEN_CALL_TIMEOUT_SECS", "1"),
        ("RAMEN_BUCKET", dir.to_str().unwrap()),
    ]));
    a.sidecar.load().await.unwrap();
    let r = rpc(
        &a,
        "k1",
        "tools/call",
        json!({"name": "slow", "arguments": {}}),
    )
    .await
    .1;
    assert!(
        r["error"]["message"]
            .as_str()
            .unwrap()
            .contains("timed out")
    );
    assert!(!a.sidecar.alive().await);
    let _ = std::fs::remove_dir_all(&dir);
}
