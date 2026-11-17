//! D92 (CONTRACTS §16.7): an `Admin/Reload` that carries `revoked` drops a key and refuses listed tokens at once —
//! no restart, the env (the deploy Secret) still naming the key — and a later plain reload keeps the overlay.
use ramen_node::config::Config;
use ramen_node::grpc::{app_with, routes};
use ramen_node::pb::admin_client::AdminClient;
use ramen_node::pb::mcp_client::McpClient;
use ramen_node::pb::{JsonRpc, ReloadRequest};
use ramen_node::revoke::key_hash;
use serde_json::{Value, json};
use std::net::SocketAddr;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::transport::{Endpoint, Server};
use tonic::{Code, Request, Status};

async fn serve(vars: Vec<(String, String)>) -> (SocketAddr, tokio::sync::oneshot::Sender<()>) {
    let app = app_with(
        Config::from_vars(vars.clone().into_iter()).unwrap(),
        Box::new(move || Config::from_vars(vars.clone().into_iter())),
    )
    .await;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let (tx, rx) = tokio::sync::oneshot::channel::<()>();
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
    (addr, tx)
}

async fn reload(addr: SocketAddr, revoked: &str) -> Result<(), Status> {
    let ch = Endpoint::from_shared(format!("http://{addr}"))
        .unwrap()
        .connect()
        .await
        .unwrap();
    let mut req = Request::new(ReloadRequest {
        revoked: revoked.as_bytes().to_vec(),
    });
    req.metadata_mut()
        .insert("x-ramen-admin-key", "adm".parse().unwrap());
    AdminClient::new(ch).reload(req).await.map(|_| ())
}

async fn list(addr: SocketAddr, bearer: &str) -> Result<Value, Status> {
    let ch = Endpoint::from_shared(format!("http://{addr}"))
        .unwrap()
        .connect()
        .await
        .unwrap();
    let mut req = Request::new(JsonRpc {
        body: json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            .to_string()
            .into_bytes(),
    });
    req.metadata_mut()
        .insert("authorization", format!("Bearer {bearer}").parse().unwrap());
    let out = McpClient::new(ch).call(req).await?.into_inner().body;
    Ok(serde_json::from_slice(&out).unwrap())
}

fn code(r: &Result<Value, Status>) -> Code {
    r.as_ref().err().map_or(Code::Ok, Status::code)
}

#[tokio::test]
async fn a_pushed_revocation_takes_effect_at_the_reload() {
    let dir = std::env::temp_dir().join(format!("ramen-d92-{}", std::process::id()));
    std::fs::create_dir_all(dir.join("ramen_runtime")).unwrap();
    std::fs::create_dir_all(dir.join("bucket")).unwrap();
    std::fs::write(
        dir.join("ramen_runtime/__main__.py"),
        r#"import json, sys
for line in sys.stdin:
    m = json.loads(line)
    r = {"tools": [{"name": "calc"}], "prompts": [], "resources": [], "errors": []}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
"#,
    )
    .unwrap();
    let vars: Vec<(String, String)> = [
        ("RAMEN_MCP_KEYS", "rmk_gone,rmk_live"),
        ("RAMEN_ADMIN_KEY", "adm"),
        ("RAMEN_GROUP", "demo"),
        ("RAMEN_PYTHON", "python3"),
        ("RAMEN_PYTHONPATH", dir.to_str().unwrap()),
        ("RAMEN_BUCKET", dir.join("bucket").to_str().unwrap()),
        ("RAMEN_SESSION_SECRET", "zone-secret"),
        ("RAMEN_OAUTH_ISSUER", "https://console.example"),
    ]
    .iter()
    .map(|(k, v)| (k.to_string(), v.to_string()))
    .collect();
    let (addr, _stop) = serve(vars).await;
    reload(addr, "").await.unwrap();
    let key = ramen_node::session::Sessions::new(Some("zone-secret"), 60).derived_key("oauth");
    let now = ramen_node::session::now();
    let token = |sub: &str, jti: &str, iat: u64| {
        ramen_node::token::mint(
            &json!({"iss": "https://console.example", "sub": sub, "aud": "mcp:demo:local",
                    "scope": "mcp:demo:local", "iat": iat, "exp": now + 600, "jti": jti}),
            &key,
        )
    };
    let (t1, t2, t3) = (
        token("u1", "j1", now - 5),
        token("u2", "j2", now - 5),
        token("u3", "j3", now - 5),
    );
    for b in ["rmk_gone", "rmk_live", &t1, &t2, &t3] {
        assert_eq!(code(&list(addr, b).await), Code::Ok, "{b}");
    }
    // the console's push: one key and one token id
    let pushed = format!(
        r#"{{"keys":["{}"],"jti":["j1"],"sub":{{}}}}"#,
        key_hash("rmk_gone")
    );
    reload(addr, &pushed).await.unwrap();
    assert_eq!(code(&list(addr, "rmk_gone").await), Code::Unauthenticated);
    assert_eq!(code(&list(addr, &t1).await), Code::Unauthenticated);
    for b in ["rmk_live", &t2, &t3] {
        assert_eq!(code(&list(addr, b).await), Code::Ok, "{b}");
    }
    // a plain reload (a deploy's, or another push's) re-reads the env, which still names the key: the overlay stays
    reload(addr, "").await.unwrap();
    assert_eq!(code(&list(addr, "rmk_gone").await), Code::Unauthenticated);
    assert_eq!(code(&list(addr, &t1).await), Code::Unauthenticated);
    // a subject cut-off: tokens issued at or before it are refused, one minted after it passes
    reload(addr, &format!(r#"{{"sub":{{"u2":{now}}}}}"#))
        .await
        .unwrap();
    assert_eq!(code(&list(addr, &t2).await), Code::Unauthenticated);
    assert_eq!(
        code(&list(addr, &token("u2", "j9", now + 1)).await),
        Code::Ok
    );
    assert_eq!(code(&list(addr, &t1).await), Code::Unauthenticated); // the union: j1 still listed
    assert_eq!(code(&list(addr, &t3).await), Code::Ok);
    // a malformed overlay is refused and changes nothing
    let bad = reload(addr, "not json").await.unwrap_err();
    assert_eq!(bad.code(), Code::InvalidArgument);
    assert_eq!(code(&list(addr, "rmk_live").await), Code::Ok);
    std::fs::remove_dir_all(&dir).ok();
}
