//! axum routes: `/mcp` (+ `RAMEN_MCP_PATH_PREFIX` alias), `/healthz`, `/readyz`, `/metrics`, `/admin/reload`.
use crate::auth;
use crate::config::Config;
use crate::log::emit;
use crate::mcp;
use crate::metrics::Metrics;
use crate::sidecar::{RpcErr, Sidecar};
use axum::Router;
use axum::body::Bytes;
use axum::extract::{ConnectInfo, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use serde_json::{Value, json};
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::{RwLock, Semaphore};

pub type ConfigSource = Box<dyn Fn() -> Result<Config, String> + Send + Sync>;

pub struct App {
    pub cfg: RwLock<Config>,
    pub sidecar: Arc<Sidecar>,
    pub sem: Semaphore,
    pub metrics: Metrics,
    /// Re-read on `/admin/reload` (env + yaml in production; injectable for tests).
    pub config_source: ConfigSource,
    /// `RAMEN_MCP_PATH_PREFIX`: fixed at startup, an ALB forwards `/mcp/<group>/<zone>` unchanged (§8).
    pub mcp_alias: Option<String>,
}

pub type Shared = Arc<App>;

pub fn app(cfg: Config) -> Shared {
    app_with(cfg, Box::new(Config::load))
}

pub fn app_with(cfg: Config, config_source: ConfigSource) -> Shared {
    let sidecar = Sidecar::new(cfg.clone());
    Arc::new(App {
        sem: Semaphore::new(cfg.max_inflight),
        mcp_alias: cfg.mcp_path_prefix.clone(),
        cfg: RwLock::new(cfg),
        sidecar,
        metrics: Metrics::default(),
        config_source,
    })
}

pub fn router(state: Shared) -> Router {
    let r = Router::new().route("/mcp", post(mcp_post));
    let r = match &state.mcp_alias {
        Some(p) => r.route(p, post(mcp_post)),
        None => r,
    };
    r.route("/healthz", get(|| async { "ok" }))
        .route("/readyz", get(readyz))
        .route("/metrics", get(metrics))
        .route("/admin/reload", post(admin_reload))
        .with_state(state)
}

fn rpc_error(status: StatusCode, id: Value, code: i64, msg: &str) -> Response {
    (
        status,
        axum::Json(json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": msg}})),
    )
        .into_response()
}

async fn mcp_post(
    State(app): State<Shared>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let cfg = app.cfg.read().await.clone();
    let ip = auth::client_ip(&cfg, peer, &headers);
    if !auth::ip_allowed(&cfg, ip) {
        return rpc_error(StatusCode::FORBIDDEN, Value::Null, -32000, "ip not allowed");
    }
    let Some(key_id) = auth::check_key(&cfg, &headers) else {
        return rpc_error(
            StatusCode::UNAUTHORIZED,
            Value::Null,
            -32001,
            "unauthorized",
        );
    };
    let Ok(req) = serde_json::from_slice::<Value>(&body) else {
        return rpc_error(StatusCode::BAD_REQUEST, Value::Null, -32700, "parse error");
    };
    let (id, method) = (
        req.get("id").cloned().unwrap_or(Value::Null),
        req["method"].as_str().unwrap_or("").to_string(),
    );
    if method.is_empty() {
        return rpc_error(
            StatusCode::BAD_REQUEST,
            id,
            -32600,
            "invalid request: method missing",
        );
    }
    let params = req.get("params").cloned().unwrap_or(json!({}));
    let Ok(_permit) = app.sem.try_acquire() else {
        app.metrics.record(false);
        return rpc_error(
            StatusCode::SERVICE_UNAVAILABLE,
            id,
            -32000,
            "busy: max inflight reached",
        );
    };
    let t0 = Instant::now();
    let out = mcp::dispatch(&app.sidecar, &cfg.blocked, &method, &params).await;
    let (status, ok) = match &out {
        Ok(Some(r)) => (
            200,
            !r.get("isError").and_then(Value::as_bool).unwrap_or(false),
        ),
        Ok(None) => (202, true),
        Err(_) => (200, false),
    };
    app.metrics.record(ok);
    let mut line = json!({"ip": ip, "group": cfg.group, "zone": cfg.zone, "env": cfg.env, "method": method, "name": mcp::target_name(&method, &params),
                          "status": if ok { "ok" } else { "error" }, "http": status, "ms": t0.elapsed().as_millis() as u64, "key_id": key_id});
    if cfg.verbose {
        line["request"] = req.clone();
        line["response"] = match &out {
            Ok(v) => v.clone().unwrap_or(Value::Null),
            Err(e) => json!({"code": e.code, "message": e.message}),
        };
    }
    emit("info", "mcp", line);
    match out {
        Ok(Some(result)) => {
            axum::Json(json!({"jsonrpc": "2.0", "id": id, "result": result})).into_response()
        }
        Ok(None) => StatusCode::ACCEPTED.into_response(),
        Err(RpcErr { code, message }) => rpc_error(StatusCode::OK, id, code, &message),
    }
}

async fn readyz(State(app): State<Shared>) -> Response {
    match app.sidecar.loaded().await {
        Some(l) => (StatusCode::OK, format!("ready since {}", l.at)).into_response(),
        None => (StatusCode::SERVICE_UNAVAILABLE, "runtime not loaded").into_response(),
    }
}

async fn metrics(State(app): State<Shared>) -> Response {
    let max = app.cfg.read().await.max_inflight;
    let loaded = app.sidecar.loaded().await;
    let packages = loaded.as_ref().map(|l| json!({"tools": len(&l.result, "tools"), "resources": len(&l.result, "resources"), "prompts": len(&l.result, "prompts"), "errors": len(&l.result, "errors")})).unwrap_or(Value::Null);
    axum::Json(app.metrics.snapshot(
        max - app.sem.available_permits(),
        max,
        app.sidecar.alive().await,
        loaded.map(|l| l.at),
        packages,
    ))
    .into_response()
}

fn len(v: &Value, k: &str) -> usize {
    v[k].as_array().map_or(0, Vec::len)
}

async fn admin_reload(
    State(app): State<Shared>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
) -> Response {
    let cfg = app.cfg.read().await.clone();
    if !auth::admin_ip_allowed(&cfg, auth::client_ip(&cfg, peer, &headers))
        || !auth::check_admin(&cfg, &headers)
    {
        return (
            StatusCode::FORBIDDEN,
            axum::Json(json!({"error": "forbidden"})),
        )
            .into_response();
    }
    match (app.config_source)() {
        Ok(fresh) => {
            crate::log::set_file(fresh.log_file.clone());
            app.sidecar.set_config(fresh.clone()).await;
            *app.cfg.write().await = fresh;
        }
        Err(e) => emit(
            "warn",
            "config reload failed, keeping current",
            json!({"error": e}),
        ),
    }
    match app.sidecar.load().await {
        Ok(v) => {
            emit(
                "info",
                "reloaded",
                json!({"tools": len(&v, "tools"), "errors": len(&v, "errors")}),
            );
            axum::Json(v).into_response()
        }
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            axum::Json(json!({"error": e.message})),
        )
            .into_response(),
    }
}

impl IntoResponse for RpcErr {
    fn into_response(self) -> Response {
        ([(header::CONTENT_TYPE, "application/json")], json!({"jsonrpc": "2.0", "id": null, "error": {"code": self.code, "message": self.message}}).to_string()).into_response()
    }
}
