//! gRPC surface tests over real TCP (peer address visible); the ones marked `py` spawn the real Python runtime
//! against the demo fixture.
use ramen_node::config::Config;
use ramen_node::grpc::{Shared, app_with, routes};
use ramen_node::pb::admin_client::AdminClient;
use ramen_node::pb::mcp_client::McpClient;
use ramen_node::pb::{JsonRpc, MetricsRequest, ReloadRequest};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use tokio::sync::oneshot;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::transport::{Channel, Endpoint, Server};
use tonic::{Code, Request, Status};
use tonic_health::pb::HealthCheckRequest;
use tonic_health::pb::health_check_response::ServingStatus;
use tonic_health::pb::health_client::HealthClient;

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

async fn app(c: Config) -> Shared {
    let mut reloaded = c.clone();
    reloaded.mcp_keys.push("k2".into());
    app_with(c, Box::new(move || Ok(reloaded.clone()))).await
}

struct Node {
    app: Shared,
    addr: SocketAddr,
    _stop: oneshot::Sender<()>,
}

async fn serve(app: Shared) -> Node {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let (tx, rx) = oneshot::channel::<()>();
    let r = routes(&app);
    tokio::spawn(async move {
        Server::builder()
            .add_routes(r)
            .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async {
                let _ = rx.await;
            })
            .await
            .unwrap();
    });
    Node {
        app,
        addr,
        _stop: tx,
    }
}

async fn channel(addr: SocketAddr) -> Channel {
    Endpoint::from_shared(format!("http://{addr}"))
        .unwrap()
        .connect()
        .await
        .unwrap()
}

fn with_md<T>(msg: T, md: &[(&str, &str)]) -> Request<T> {
    let mut req = Request::new(msg);
    for (k, v) in md {
        let key = tonic::metadata::MetadataKey::from_bytes(k.as_bytes()).unwrap();
        req.metadata_mut().insert(key, v.parse().unwrap());
    }
    req
}

/// `Mcp/Call` with raw bytes; `Ok(Null)` for an empty body (notification).
async fn call_raw(node: &Node, md: &[(&str, &str)], body: &[u8]) -> Result<Value, Status> {
    let mut c = McpClient::new(channel(node.addr).await).max_encoding_message_size(usize::MAX);
    let out = c
        .call(with_md(
            JsonRpc {
                body: body.to_vec(),
            },
            md,
        ))
        .await?
        .into_inner()
        .body;
    if out.is_empty() {
        return Ok(Value::Null);
    }
    Ok(serde_json::from_slice(&out).expect("json body"))
}

async fn rpc(node: &Node, key: &str, method: &str, params: Value) -> Result<Value, Status> {
    let body = json!({"jsonrpc": "2.0", "id": 1, "method": method, "params": params});
    call_raw(
        node,
        &[("authorization", &format!("Bearer {key}"))],
        body.to_string().as_bytes(),
    )
    .await
}

async fn health(node: &Node, service: &str) -> ServingStatus {
    let mut hc = HealthClient::new(channel(node.addr).await);
    let st = hc
        .check(HealthCheckRequest {
            service: service.into(),
        })
        .await
        .unwrap()
        .into_inner()
        .status;
    ServingStatus::try_from(st).unwrap()
}

async fn reload(node: &Node, md: &[(&str, &str)]) -> Result<Value, Status> {
    let mut c = AdminClient::new(channel(node.addr).await);
    let out = c.reload(with_md(ReloadRequest {}, md)).await?.into_inner();
    Ok(serde_json::from_slice(&out.json).unwrap())
}

async fn metrics(node: &Node) -> Value {
    let mut c = AdminClient::new(channel(node.addr).await);
    let out = c
        .metrics(with_md(MetricsRequest {}, &[("x-ramen-admin-key", "adm")]))
        .await
        .unwrap()
        .into_inner();
    serde_json::from_slice(&out.json).unwrap()
}

fn code(r: &Result<Value, Status>) -> Code {
    r.as_ref().err().map_or(Code::Ok, Status::code)
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
    let n = serve(app(cfg(&[("RAMEN_ALLOWED_CIDRS", "127.0.0.0/8")])).await).await;
    assert_eq!(health(&n, "").await, ServingStatus::NotServing);
    assert_eq!(health(&n, "ramen.v1.Mcp").await, ServingStatus::NotServing);
    assert_eq!(health(&n, "ramen.v1.Admin").await, ServingStatus::Serving);
    let m = metrics(&n).await;
    assert_eq!(
        (
            m["load"].as_str(),
            m["sidecar_alive"].as_bool(),
            m["inflight"].as_u64()
        ),
        (Some("low"), Some(false), Some(0))
    );
    let ping = json!({"jsonrpc": "2.0", "id": 1, "method": "ping"}).to_string();
    assert_eq!(
        code(&call_raw(&n, &[], ping.as_bytes()).await),
        Code::Unauthenticated
    );
    assert_eq!(
        code(&call_raw(&n, &[("authorization", "Bearer nope")], ping.as_bytes()).await),
        Code::Unauthenticated
    );
    assert_eq!(
        code(&call_raw(&n, &[("authorization", "Basic k1")], ping.as_bytes()).await),
        Code::Unauthenticated
    );
    let auth = [("authorization", "Bearer k1")];
    let b = call_raw(&n, &auth, b"{nope").await.unwrap();
    assert_eq!(b["error"]["code"], -32700);
    assert_eq!(
        rpc(&n, "k1", "", json!({})).await.unwrap()["error"]["code"],
        -32600
    );
    let b = rpc(&n, "k1", "initialize", json!({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})).await.unwrap();
    assert_eq!(
        (
            b["result"]["protocolVersion"].as_str(),
            b["result"]["serverInfo"]["name"].as_str()
        ),
        (Some("2025-06-18"), Some("ramen-node"))
    );
    assert_eq!(
        rpc(&n, "k1", "ping", json!({})).await.unwrap()["result"],
        json!({})
    );
    let note = json!({"jsonrpc": "2.0", "method": "notifications/initialized"}).to_string();
    assert_eq!(
        call_raw(&n, &auth, note.as_bytes()).await.unwrap(),
        Value::Null
    );
    assert_eq!(
        rpc(&n, "k1", "tools/list", json!({})).await.unwrap()["error"]["code"],
        -32002
    );
    assert_eq!(
        rpc(&n, "k1", "nope/x", json!({})).await.unwrap()["error"]["code"],
        -32601
    );
    assert_eq!(
        rpc(&n, "k1", "tools/call", json!({"name": 1}))
            .await
            .unwrap()["error"]["code"],
        -32602
    );
    assert_eq!(
        rpc(&n, "k1", "resources/read", json!({})).await.unwrap()["error"]["code"],
        -32602
    );
    assert_eq!(code(&reload(&n, &[]).await), Code::Unauthenticated);
    assert_eq!(
        code(&reload(&n, &[("x-ramen-admin-key", "wrong")]).await),
        Code::Unauthenticated
    );
    // Session is reserved
    let mut c = McpClient::new(channel(n.addr).await);
    let st = c
        .session(with_md(tokio_stream::iter(vec![JsonRpc::default()]), &auth))
        .await
        .err()
        .unwrap();
    assert_eq!(st.code(), Code::Unimplemented);
    // 4 MiB message limit (codec)
    let big = vec![b' '; ramen_node::grpc::MAX_MESSAGE_BYTES + 1];
    assert_eq!(code(&call_raw(&n, &auth, &big).await), Code::OutOfRange);
    // inflight bound
    let permit = n
        .app
        .sem
        .acquire_many(n.app.cfg.read().await.max_inflight as u32)
        .await
        .unwrap();
    assert_eq!(
        code(&rpc(&n, "k1", "ping", json!({})).await),
        Code::ResourceExhausted
    );
    drop(permit);
    let m = metrics(&n).await;
    assert!(m["total"].as_u64().unwrap() >= 6 && m["errors"].as_u64().unwrap() >= 4);
}

#[tokio::test]
async fn cidr_denies_mcp_but_admin_has_its_own_list() {
    let n = serve(app(cfg(&[("RAMEN_ALLOWED_CIDRS", "10.0.0.0/8")])).await).await;
    assert_eq!(
        code(&rpc(&n, "k1", "ping", json!({})).await),
        Code::PermissionDenied
    );
    // admin is not bound by the MCP allowlist: whatever the load outcome, it is never PERMISSION_DENIED
    assert_ne!(
        code(&reload(&n, &[("x-ramen-admin-key", "adm")]).await),
        Code::PermissionDenied
    );
    let n = serve(app(cfg(&[("RAMEN_ADMIN_CIDRS", "10.0.0.0/8")])).await).await;
    assert_eq!(
        code(&reload(&n, &[("x-ramen-admin-key", "adm")]).await),
        Code::PermissionDenied
    );
    let mut c = AdminClient::new(channel(n.addr).await);
    assert_eq!(
        c.metrics(with_md(MetricsRequest {}, &[("x-ramen-admin-key", "adm")]))
            .await
            .err()
            .unwrap()
            .code(),
        Code::PermissionDenied
    );
    assert_eq!(
        rpc(&n, "k1", "ping", json!({})).await.unwrap()["result"],
        json!({})
    );
}

#[tokio::test]
async fn trust_proxy_uses_forwarded_ip() {
    let ping = json!({"jsonrpc": "2.0", "id": 1, "method": "ping"}).to_string();
    let xff = [
        ("authorization", "Bearer k1"),
        ("x-forwarded-for", "10.1.1.1"),
    ];
    let strict = serve(app(cfg(&[("RAMEN_ALLOWED_CIDRS", "10.0.0.0/8")])).await).await;
    assert_eq!(
        code(&call_raw(&strict, &xff, ping.as_bytes()).await),
        Code::PermissionDenied
    );
    // legacy RAMEN_TRUST_PROXY=1 = one hop = the right-most (proxy-appended) entry
    let trusting = serve(
        app(cfg(&[
            ("RAMEN_ALLOWED_CIDRS", "10.0.0.0/8"),
            ("RAMEN_TRUST_PROXY", "1"),
        ]))
        .await,
    )
    .await;
    assert_eq!(
        call_raw(&trusting, &xff, ping.as_bytes()).await.unwrap()["result"],
        json!({})
    );
}

/// Claims review rows 14/36: the allowlist must not be spoofable by a caller-supplied `x-forwarded-for`.
/// The real peer here is 127.0.0.1, which the allowlist excludes, so anything that ends up trusting a
/// caller-chosen entry shows up as an allowed call.
#[tokio::test]
async fn forwarded_for_spoof_is_refused_and_hop_count_is_per_provider() {
    let allowed = "203.0.113.9"; // inside RAMEN_ALLOWED_CIDRS
    let real = "198.51.100.1"; // a real client outside it
    // GCP external ALB appends "<client>, <lb>": the client is the 2nd entry from the right.
    let gcp = xff_node("2", "203.0.113.0/24").await;
    assert_eq!(
        xff_status(&gcp, &format!("{allowed}, {real}, 130.211.0.5")).await,
        Code::PermissionDenied,
        "a left-most entry the caller chose must not be trusted"
    );
    assert_eq!(
        xff_status(&gcp, &format!("{real}, {allowed}, 130.211.0.5")).await,
        Code::Ok,
        "the entry the GCP load balancer appends the client as must be honoured"
    );
    // AWS ALB appends "<client>": the client is the right-most entry.
    let aws = xff_node("1", "203.0.113.0/24").await;
    assert_eq!(
        xff_status(&aws, &format!("{allowed}, {real}")).await,
        Code::PermissionDenied
    );
    assert_eq!(
        xff_status(&aws, &format!("{real}, {allowed}")).await,
        Code::Ok
    );
    // Too short or malformed for the configured hop count → the peer address, not a header value.
    for xff in [allowed, "junk, 203.0.113.9", ""] {
        assert_eq!(
            xff_status(&gcp, xff).await,
            Code::PermissionDenied,
            "fell back to a header value for {xff:?}"
        );
    }
    // ... and that fallback really is the peer (127.0.0.1), not a blanket denial.
    let loopback = xff_node("2", "127.0.0.0/8").await;
    assert_eq!(xff_status(&loopback, allowed).await, Code::Ok);
    // Proxy trust off (the default) ignores the header entirely.
    let off = xff_node("0", "203.0.113.0/24").await;
    assert_eq!(
        xff_status(&off, &format!("{real}, {allowed}, 130.211.0.5")).await,
        Code::PermissionDenied
    );
    assert_eq!(xff_status(&off, allowed).await, Code::PermissionDenied);
}

async fn xff_node(hops: &str, cidrs: &str) -> Node {
    let c = cfg(&[
        ("RAMEN_ALLOWED_CIDRS", cidrs),
        ("RAMEN_TRUST_PROXY_HOPS", hops),
    ]);
    serve(app(c).await).await
}

/// `Mcp/Call ping` with a good key and this `x-forwarded-for`, as a gRPC code.
async fn xff_status(node: &Node, xff: &str) -> Code {
    let ping = json!({"jsonrpc": "2.0", "id": 1, "method": "ping"}).to_string();
    let md = [("authorization", "Bearer k1"), ("x-forwarded-for", xff)];
    code(&call_raw(node, &md, ping.as_bytes()).await)
}

/// Claims review, incidental: `Admin/Reload` may lower `RAMEN_MAX_INFLIGHT` below the permits the (never
/// resized) semaphore already holds, which used to make `metrics_json` subtract with overflow.
#[tokio::test]
async fn reload_to_a_lower_max_inflight_keeps_metrics_readable() {
    let start = cfg(&[
        ("RAMEN_MAX_INFLIGHT", "4"),
        ("RAMEN_PYTHON", "/nonexistent/python"),
    ]);
    let lower = cfg(&[
        ("RAMEN_MAX_INFLIGHT", "1"),
        ("RAMEN_PYTHON", "/nonexistent/python"),
    ]);
    let n = serve(app_with(start, Box::new(move || Ok(lower.clone()))).await).await;
    // the load fails (no interpreter), but the config swap happens regardless
    assert_eq!(
        code(&reload(&n, &[("x-ramen-admin-key", "adm")]).await),
        Code::Internal
    );
    assert_eq!(n.app.cfg.read().await.max_inflight, 1);
    let m = metrics(&n).await;
    assert_eq!(
        (m["inflight"].as_u64(), m["load"].as_str()),
        (Some(0), Some("low"))
    );
    let permit = n.app.sem.acquire_many(3).await.unwrap();
    let m = metrics(&n).await;
    assert_eq!(
        m["inflight"].as_u64(),
        Some(3),
        "measured against the live semaphore"
    );
    drop(permit);
}

/// The reflection services run ahead of every guard, so whether they are registered is a deliberate choice
/// (`RAMEN_REFLECTION`, default on; the worker chart turns it off).
#[tokio::test]
async fn reflection_is_on_by_default_and_switchable() {
    let on = serve(app(cfg(&[])).await).await;
    let services = list_services(&on).await.expect("reflection on by default");
    for want in ["ramen.v1.Mcp", "ramen.v1.Admin", "grpc.health.v1.Health"] {
        assert!(services.iter().any(|s| s == want), "{want} in {services:?}");
    }
    let off = serve(app(cfg(&[("RAMEN_REFLECTION", "0")])).await).await;
    assert_eq!(
        list_services(&off).await.err().map(|e| e.code()),
        Some(Code::Unimplemented)
    );
    // ... and the rest of the surface is untouched.
    assert_eq!(health(&off, "ramen.v1.Admin").await, ServingStatus::Serving);
    assert_eq!(
        rpc(&off, "k1", "ping", json!({})).await.unwrap()["result"],
        json!({})
    );
}

async fn list_services(node: &Node) -> Result<Vec<String>, Status> {
    use tonic_reflection::pb::v1::ServerReflectionRequest;
    use tonic_reflection::pb::v1::server_reflection_client::ServerReflectionClient;
    use tonic_reflection::pb::v1::server_reflection_request::MessageRequest;
    use tonic_reflection::pb::v1::server_reflection_response::MessageResponse;
    let mut c = ServerReflectionClient::new(channel(node.addr).await);
    let req = ServerReflectionRequest {
        host: String::new(),
        message_request: Some(MessageRequest::ListServices(String::new())),
    };
    let mut stream = c
        .server_reflection_info(tokio_stream::iter(vec![req]))
        .await?
        .into_inner();
    let msg = stream.message().await?.expect("one reflection response");
    match msg.message_response {
        Some(MessageResponse::ListServicesResponse(r)) => {
            Ok(r.service.into_iter().map(|s| s.name).collect())
        }
        other => panic!("unexpected reflection response: {other:?}"),
    }
}

#[tokio::test]
async fn tls_serves_h2_and_rejects_plaintext() {
    let dir = std::env::temp_dir().join(format!("ramen-tls-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let ck = rcgen::generate_simple_self_signed(vec!["localhost".to_string()]).unwrap();
    let (cert, key) = (dir.join("cert.pem"), dir.join("key.pem"));
    std::fs::write(&cert, ck.cert.pem()).unwrap();
    std::fs::write(&key, ck.signing_key.serialize_pem()).unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let c = cfg(&[
        ("RAMEN_TLS_CERT", cert.to_str().unwrap()),
        ("RAMEN_TLS_KEY", key.to_str().unwrap()),
        ("RAMEN_BUCKET", dir.to_str().unwrap()),
    ]);
    let (tx, rx) = oneshot::channel::<()>();
    let server = tokio::spawn(ramen_node::server::run(c, listener, async move {
        let _ = rx.await;
    }));
    let tls = tonic::transport::ClientTlsConfig::new()
        .ca_certificate(tonic::transport::Certificate::from_pem(ck.cert.pem()))
        .domain_name("localhost");
    let ch = Endpoint::from_shared(format!("https://{addr}"))
        .unwrap()
        .tls_config(tls)
        .unwrap()
        .connect()
        .await
        .unwrap();
    let mut mc = McpClient::new(ch);
    let body = json!({"jsonrpc": "2.0", "id": 1, "method": "ping"}).to_string();
    let out = mc
        .call(with_md(
            JsonRpc {
                body: body.into_bytes(),
            },
            &[("authorization", "Bearer k1")],
        ))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        serde_json::from_slice::<Value>(&out.body).unwrap()["result"],
        json!({})
    );
    let plain = Endpoint::from_shared(format!("http://{addr}"))
        .unwrap()
        .connect_lazy();
    assert!(
        HealthClient::new(plain)
            .check(HealthCheckRequest::default())
            .await
            .is_err()
    );
    tx.send(()).unwrap();
    server.await.unwrap();
    std::fs::remove_dir_all(&dir).unwrap();
}

#[tokio::test]
async fn py_spawn_missing_python_is_internal_error() {
    let n = serve(app(cfg(&[("RAMEN_PYTHON", "/nonexistent/python")])).await).await;
    let b = rpc(&n, "k1", "tools/call", json!({"name": "x"}))
        .await
        .unwrap();
    assert_eq!(b["error"]["code"], -32603);
    assert!(b["error"]["message"].as_str().unwrap().contains("spawn"));
}

#[tokio::test]
async fn py_end_to_end_with_demo_repo() {
    if !python_ok() {
        return;
    }
    let n = serve(app(cfg(&[("RAMEN_VERBOSE", "1")])).await).await;
    n.app.load().await.unwrap();
    assert_eq!(health(&n, "").await, ServingStatus::Serving);
    assert_eq!(health(&n, "ramen.v1.Mcp").await, ServingStatus::Serving);
    let tools = rpc(&n, "k1", "tools/list", json!({})).await.unwrap();
    assert_eq!(tools["result"]["tools"][0]["name"], "demo_calculator_tool");
    assert_eq!(
        tools["result"]["tools"][0]["inputSchema"]["required"],
        json!(["var1", "var2", "func"])
    );
    // C9: the declared `output` (a number) is published as an object schema wrapping `result`
    assert_eq!(
        tools["result"]["tools"][0]["outputSchema"],
        json!({"type": "object", "properties": {"result": {"type": "number"}}, "required": ["result"]})
    );
    let r = rpc(
        &n,
        "k1",
        "tools/call",
        json!({"name": "demo_calculator_tool", "arguments": {"var1": 2, "var2": 3, "func": "add"}}),
    )
    .await
    .unwrap();
    assert_eq!(
        r["result"],
        json!({"content": [{"type": "text", "text": "5"}], "structuredContent": {"result": 5}, "isError": false})
    );
    let r = rpc(&n, "k1", "tools/call", json!({"name": "demo_calculator_tool", "arguments": {"var1": 2, "var2": 0, "func": "divide"}})).await.unwrap();
    assert_eq!(r["result"]["isError"], true);
    assert_eq!(
        rpc(&n, "k1", "tools/call", json!({"name": "nope"}))
            .await
            .unwrap()["error"]["code"],
        -32004
    );
    assert_eq!(
        rpc(&n, "k1", "resources/list", json!({})).await.unwrap()["result"]["resources"][0]["uri"],
        "ramen://demo/readme"
    );
    let r = rpc(
        &n,
        "k1",
        "resources/read",
        json!({"uri": "ramen://demo/readme"}),
    )
    .await
    .unwrap();
    assert!(
        r["result"]["contents"][0]["text"]
            .as_str()
            .unwrap()
            .starts_with("# ramen-demo-mcp-group")
    );
    assert_eq!(
        rpc(&n, "k1", "prompts/list", json!({})).await.unwrap()["result"]["prompts"][0]["arguments"]
            [0]["name"],
        "request"
    );
    let r = rpc(
        &n,
        "k1",
        "prompts/get",
        json!({"name": "get_calculation_prompt", "arguments": {"request": "9*9"}}),
    )
    .await
    .unwrap();
    assert!(
        r["result"]["messages"][0]["content"]["text"]
            .as_str()
            .unwrap()
            .contains("Request: 9*9")
    );
    assert_eq!(
        rpc(
            &n,
            "k1",
            "prompts/get",
            json!({"name": "get_calculation_prompt"})
        )
        .await
        .unwrap()["error"]["code"],
        -32602
    );
    let m = metrics(&n).await;
    assert_eq!(
        (
            m["sidecar_alive"].as_bool(),
            m["packages"]["tools"].as_u64(),
            m["packages"]["errors"].as_u64()
        ),
        (Some(true), Some(1), Some(0))
    );
    assert!(m["loaded_at"].is_string());
    assert_eq!(m["manifest_hash"].as_str().map(str::len), Some(64)); // C2, from the runtime's C1 hash
    // respawn after a kill re-runs runtime.load transparently
    n.app.sidecar.kill("test").await;
    assert!(!n.app.sidecar.alive().await);
    let r = rpc(&n, "k1", "tools/call", json!({"name": "demo_calculator_tool", "arguments": {"var1": 6, "var2": 7, "func": "multiply"}})).await.unwrap();
    assert_eq!(r["result"]["content"][0]["text"], "42");
    // admin reload: re-reads config (k2 becomes valid) and reloads packages
    assert_eq!(
        code(&rpc(&n, "k2", "ping", json!({})).await),
        Code::Unauthenticated
    );
    let b = reload(&n, &[("x-ramen-admin-key", "adm")]).await.unwrap();
    assert_eq!(b["tools"][0]["name"], "demo_calculator_tool");
    assert_eq!(b["hash"], m["manifest_hash"]); // C1: the reload result carries the same hash
    assert_eq!(
        rpc(&n, "k2", "ping", json!({})).await.unwrap()["result"],
        json!({})
    );
}

#[tokio::test]
async fn py_idle_reaper_kills_and_call_respawns() {
    if !python_ok() {
        return;
    }
    let n = serve(app(cfg(&[("RAMEN_SIDECAR_IDLE_SECS", "1")])).await).await;
    n.app.sidecar.start_reaper();
    n.app.load().await.unwrap();
    assert!(n.app.sidecar.alive().await);
    for _ in 0..40 {
        tokio::time::sleep(std::time::Duration::from_millis(250)).await;
        if !n.app.sidecar.alive().await {
            break;
        }
    }
    assert!(
        !n.app.sidecar.alive().await,
        "reaper should have killed the idle sidecar"
    );
    let r = rpc(
        &n,
        "k1",
        "tools/call",
        json!({"name": "demo_calculator_tool", "arguments": {"var1": 1, "var2": 1, "func": "add"}}),
    )
    .await
    .unwrap();
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
    let n = serve(
        app(cfg(&[
            ("RAMEN_CALL_TIMEOUT_SECS", "1"),
            ("RAMEN_BUCKET", dir.to_str().unwrap()),
        ]))
        .await,
    )
    .await;
    n.app.load().await.unwrap();
    let r = rpc(
        &n,
        "k1",
        "tools/call",
        json!({"name": "slow", "arguments": {}}),
    )
    .await
    .unwrap();
    assert!(
        r["error"]["message"]
            .as_str()
            .unwrap()
            .contains("timed out")
    );
    assert!(!n.app.sidecar.alive().await);
    let _ = std::fs::remove_dir_all(&dir);
}

/// A fake `ramen_runtime` that echoes `RAMEN_BUCKET_URI` in a `sync` summary: proves the node passes
/// the URI to the sidecar env and that `Admin/Reload` returns whatever the runtime reports (and flips health).
#[tokio::test]
async fn admin_reload_returns_runtime_sync_summary() {
    let dir = std::env::temp_dir().join(format!("ramen-fake-rt-{}", std::process::id()));
    std::fs::create_dir_all(dir.join("ramen_runtime")).unwrap();
    std::fs::write(
        dir.join("ramen_runtime/__main__.py"),
        r#"import json, os, sys
for line in sys.stdin:
    m = json.loads(line)
    r = {"tools": [], "resources": [], "prompts": [], "errors": [], "sync": {"uri": os.environ.get("RAMEN_BUCKET_URI"), "downloaded": 2}} if m["method"] == "runtime.load" else {"ok": True}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
"#,
    )
    .unwrap();
    let pp = dir.display().to_string();
    let n = serve(
        app(cfg(&[
            ("RAMEN_PYTHON", "python3"),
            ("RAMEN_PYTHONPATH", &pp),
            ("RAMEN_BUCKET_URI", "gs://groups/demo"),
        ]))
        .await,
    )
    .await;
    assert_eq!(health(&n, "").await, ServingStatus::NotServing);
    let v = reload(&n, &[("x-ramen-admin-key", "adm")]).await.unwrap();
    assert_eq!(
        v["sync"],
        json!({"uri": "gs://groups/demo", "downloaded": 2})
    );
    assert_eq!(health(&n, "").await, ServingStatus::Serving);
    std::fs::remove_dir_all(&dir).unwrap();
}

/// CONTRACTS §9: `RAMEN_BLOCKED` (deploy-scoped) hides names from the list calls and answers -32601 on calls;
/// a resource blocked by name is also unreadable by URI. Fake runtime, no demo repo needed.
#[tokio::test]
async fn blocked_names_are_hidden_and_unreachable() {
    let dir = std::env::temp_dir().join(format!("ramen-blocked-{}", std::process::id()));
    std::fs::create_dir_all(dir.join("ramen_runtime")).unwrap();
    std::fs::create_dir_all(dir.join("bucket/.ramen")).unwrap();
    std::fs::write(
        dir.join("ramen_runtime/__main__.py"),
        r#"import json, sys
LOAD = {"tools": [{"name": "calc"}, {"name": "secret_tool"}], "prompts": [{"name": "p1"}, {"name": "p2"}],
        "resources": [{"name": "readme", "uri": "ramen://demo/readme"}, {"name": "other", "uri": "ramen://demo/other"}], "errors": []}
for line in sys.stdin:
    m = json.loads(line)
    r = LOAD if m["method"] == "runtime.load" else {"called": m["method"], "params": m["params"]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
"#,
    )
    .unwrap();
    std::fs::write(
        dir.join("bucket/.ramen/env"),
        "RAMEN_BLOCKED=secret_tool,p2,readme\n",
    )
    .unwrap();
    let pp = dir.display().to_string();
    let bucket = dir.join("bucket").display().to_string();
    let n = serve(
        app(cfg(&[
            ("RAMEN_PYTHON", "python3"),
            ("RAMEN_PYTHONPATH", &pp),
            ("RAMEN_BUCKET", &bucket),
        ]))
        .await,
    )
    .await;
    // before reload: env process config has no blocked list; the deploy file is applied on Admin/Reload
    reload(&n, &[("x-ramen-admin-key", "adm")]).await.unwrap();
    let v = rpc(&n, "k1", "tools/list", json!({})).await.unwrap();
    assert_eq!(
        v["result"]["tools"],
        json!([{"name": "calc"}, {"name": "secret_tool"}])
    );
    // simulate the console deploy: fresh config source reads the deploy file
    let vars: Vec<(String, String)> = [
        ("RAMEN_MCP_KEYS", "k1"),
        ("RAMEN_ADMIN_KEY", "adm"),
        ("RAMEN_PYTHON", "python3"),
        ("RAMEN_PYTHONPATH", pp.as_str()),
        ("RAMEN_BUCKET", bucket.as_str()),
    ]
    .iter()
    .map(|(k, v)| (k.to_string(), v.to_string()))
    .collect();
    let n = serve(
        app_with(
            Config::from_vars(vars.clone().into_iter()).unwrap(),
            Box::new(move || Config::from_vars(vars.clone().into_iter())),
        )
        .await,
    )
    .await;
    reload(&n, &[("x-ramen-admin-key", "adm")]).await.unwrap();
    let v = rpc(&n, "k1", "tools/list", json!({})).await.unwrap();
    assert_eq!(v["result"]["tools"], json!([{"name": "calc"}]));
    let v = rpc(&n, "k1", "prompts/list", json!({})).await.unwrap();
    assert_eq!(v["result"]["prompts"], json!([{"name": "p1"}]));
    let v = rpc(&n, "k1", "resources/list", json!({})).await.unwrap();
    assert_eq!(
        v["result"]["resources"],
        json!([{"name": "other", "uri": "ramen://demo/other"}])
    );
    let v = rpc(
        &n,
        "k1",
        "tools/call",
        json!({"name": "secret_tool", "arguments": {}}),
    )
    .await
    .unwrap();
    assert_eq!(v["error"]["code"], -32601, "{v}");
    let v = rpc(
        &n,
        "k1",
        "tools/call",
        json!({"name": "calc", "arguments": {"a": 1}}),
    )
    .await
    .unwrap();
    assert_eq!(v["result"]["called"], "runtime.call_tool");
    let v = rpc(&n, "k1", "prompts/get", json!({"name": "p2"}))
        .await
        .unwrap();
    assert_eq!(v["error"]["code"], -32601);
    let v = rpc(
        &n,
        "k1",
        "resources/read",
        json!({"uri": "ramen://demo/readme"}),
    )
    .await
    .unwrap();
    assert_eq!(
        v["error"]["code"], -32601,
        "blocked by name, read by uri: {v}"
    );
    let v = rpc(
        &n,
        "k1",
        "resources/read",
        json!({"uri": "ramen://demo/other"}),
    )
    .await
    .unwrap();
    assert_eq!(v["result"]["called"], "runtime.read_resource");
    let m = metrics(&n).await;
    assert_eq!(
        m["packages"]["tools"], 2,
        "metrics count what the runtime loaded, not what is exposed"
    );
    std::fs::remove_dir_all(&dir).unwrap();
}

/// Live GKE (0.3.2): the first `runtime.load` can fail transiently (Workload Identity / IAM propagation right after
/// the zone is attached, bucket not yet synced). The node must keep retrying the initial load on its own — the console
/// only sends `Admin/Reload` once the pod is ready, so a single attempt would leave the pod NOT_SERVING for ever.
#[tokio::test]
async fn initial_load_is_retried_until_it_succeeds() {
    let dir = std::env::temp_dir().join(format!("ramen-retry-{}", std::process::id()));
    std::fs::create_dir_all(dir.join("ramen_runtime")).unwrap();
    std::fs::write(
        dir.join("ramen_runtime/__main__.py"),
        r#"import json, os, sys
counter = os.path.join(os.environ["RAMEN_RETRY_DIR"], "loads")
for line in sys.stdin:
    m = json.loads(line)
    if m["method"] == "runtime.load":
        n = int(open(counter).read()) if os.path.exists(counter) else 0
        open(counter, "w").write(str(n + 1))
        if n < 2:
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32603, "message": "RefreshError: metadata not ready"}}), flush=True)
            continue
        r = {"tools": [], "resources": [], "prompts": [], "errors": [], "sync": {"downloaded": 1}}
    else:
        r = {"ok": True}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
"#,
    )
    .unwrap();
    let pp = dir.display().to_string();
    unsafe { std::env::set_var("RAMEN_RETRY_DIR", &pp) };
    let c = cfg(&[
        ("RAMEN_PYTHON", "python3"),
        ("RAMEN_PYTHONPATH", &pp),
        ("RAMEN_BUCKET_URI", "gs://groups/demo"),
        ("RAMEN_LOAD_RETRY_SECS", "1"),
    ]);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let (tx, rx) = oneshot::channel::<()>();
    let server = tokio::spawn(ramen_node::server::run(c, listener, async move {
        let _ = rx.await;
    }));
    let mut hc = HealthClient::new(channel(addr).await);
    let mut status = ServingStatus::NotServing;
    for _ in 0..40 {
        status = hc
            .check(HealthCheckRequest {
                service: String::new(),
            })
            .await
            .unwrap()
            .into_inner()
            .status();
        if status == ServingStatus::Serving {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
    assert_eq!(status, ServingStatus::Serving, "initial load never retried");
    assert_eq!(std::fs::read_to_string(dir.join("loads")).unwrap(), "3");
    tx.send(()).unwrap();
    server.await.unwrap();
    std::fs::remove_dir_all(&dir).unwrap();
}

/// C10 (0.7.2): `RAMEN_TOOL_ACCESS` from the deploy file narrows tools per caller kind — `key` for an `rmk_` key, a
/// token's `role` claim (missing → `mcp_user`), `super_admin` unrestricted. Hidden → `-32601`, listed-but-denied →
/// `-32003`. Same `dispatch_body` as HTTP, so this gRPC run covers both transports. Fake runtime.
#[tokio::test]
async fn tool_access_narrows_list_and_call_per_caller_kind() {
    let dir = std::env::temp_dir().join(format!("ramen-access-{}", std::process::id()));
    std::fs::create_dir_all(dir.join("ramen_runtime")).unwrap();
    std::fs::create_dir_all(dir.join("bucket/.ramen")).unwrap();
    std::fs::write(
        dir.join("ramen_runtime/__main__.py"),
        r#"import json, sys
LOAD = {"tools": [{"name": "calc"}, {"name": "secret_tool"}, {"name": "open"}], "prompts": [], "resources": [], "errors": []}
for line in sys.stdin:
    m = json.loads(line)
    r = LOAD if m["method"] == "runtime.load" else {"called": m["method"], "params": m["params"]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
"#,
    )
    .unwrap();
    std::fs::write(
        dir.join("bucket/.ramen/env"),
        r#"RAMEN_TOOL_ACCESS={"calc":{"list":["key","viewer"],"call":["key"]},"secret_tool":{"list":["group_admin"],"call":["group_admin"]}}
"#,
    )
    .unwrap();
    let pp = dir.display().to_string();
    let bucket = dir.join("bucket").display().to_string();
    let secret = "zone-secret";
    let vars: Vec<(String, String)> = [
        ("RAMEN_MCP_KEYS", "k1"),
        ("RAMEN_ADMIN_KEY", "adm"),
        ("RAMEN_GROUP", "demo"),
        ("RAMEN_PYTHON", "python3"),
        ("RAMEN_PYTHONPATH", pp.as_str()),
        ("RAMEN_BUCKET", bucket.as_str()),
        ("RAMEN_SESSION_SECRET", secret),
        ("RAMEN_OAUTH_ISSUER", "https://console.example"),
    ]
    .iter()
    .map(|(k, v)| (k.to_string(), v.to_string()))
    .collect();
    let n = serve(
        app_with(
            Config::from_vars(vars.clone().into_iter()).unwrap(),
            Box::new(move || Config::from_vars(vars.clone().into_iter())),
        )
        .await,
    )
    .await;
    reload(&n, &[("x-ramen-admin-key", "adm")]).await.unwrap();
    let key = ramen_node::session::Sessions::new(Some(secret), 60).derived_key("oauth");
    let token = |role: Option<&str>| {
        let mut c = json!({"iss": "https://console.example", "sub": "u1", "aud": "mcp:demo:local",
                           "scope": "mcp:demo:local", "exp": ramen_node::session::now() + 60});
        if let Some(r) = role {
            c["role"] = json!(r);
        }
        ramen_node::token::mint(&c, &key)
    };
    let names = |v: &Value| -> Vec<String> {
        v["result"]["tools"]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| t["name"].as_str().unwrap().to_string())
            .collect()
    };
    let call = |name: &str| json!({"name": name, "arguments": {}});
    // a key: kind `key`
    let v = rpc(&n, "k1", "tools/list", json!({})).await.unwrap();
    assert_eq!(names(&v), vec!["calc", "open"]);
    let v = rpc(&n, "k1", "tools/call", call("calc")).await.unwrap();
    assert_eq!(v["result"]["called"], "runtime.call_tool");
    let v = rpc(&n, "k1", "tools/call", call("secret_tool"))
        .await
        .unwrap();
    assert_eq!(v["error"]["code"], -32601, "hidden: {v}");
    // a viewer token: may list calc, not call it
    let viewer = token(Some("viewer"));
    let v = rpc(&n, &viewer, "tools/list", json!({})).await.unwrap();
    assert_eq!(names(&v), vec!["calc", "open"]);
    let v = rpc(&n, &viewer, "tools/call", call("calc")).await.unwrap();
    assert_eq!(
        (v["error"]["code"].as_i64(), v["error"]["message"].as_str()),
        (
            Some(-32003),
            Some("forbidden: calc is not callable for viewer")
        ),
        "{v}"
    );
    let v = rpc(&n, &viewer, "tools/call", call("open")).await.unwrap();
    assert_eq!(v["result"]["called"], "runtime.call_tool");
    // no role claim → mcp_user: calc is hidden
    let plain = token(None);
    let v = rpc(&n, &plain, "tools/list", json!({})).await.unwrap();
    assert_eq!(names(&v), vec!["open"]);
    let v = rpc(&n, &plain, "tools/call", call("calc")).await.unwrap();
    assert_eq!(v["error"]["code"], -32601, "{v}");
    // super_admin sees and calls everything
    let sa = token(Some("super_admin"));
    let v = rpc(&n, &sa, "tools/list", json!({})).await.unwrap();
    assert_eq!(names(&v), vec!["calc", "secret_tool", "open"]);
    let v = rpc(&n, &sa, "tools/call", call("secret_tool"))
        .await
        .unwrap();
    assert_eq!(v["result"]["called"], "runtime.call_tool");
    std::fs::remove_dir_all(&dir).unwrap();
}
