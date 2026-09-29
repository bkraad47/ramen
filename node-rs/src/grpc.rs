//! gRPC services (CONTRACTS §11): `ramen.v1.Mcp/Call` (JSON-RPC bytes in/out), `ramen.v1.Admin/{Reload,Metrics}`,
//! `grpc.health.v1.Health` (NOT_SERVING until the first successful `runtime.load`). Guards run inside the
//! handlers so they see the peer address and metadata: CIDR → PERMISSION_DENIED, key → UNAUTHENTICATED,
//! inflight → RESOURCE_EXHAUSTED; the 4 MiB message limit is enforced by the codec (OUT_OF_RANGE).
use crate::auth;
use crate::config::Config;
use crate::log::emit;
use crate::mcp;
use crate::metrics::Metrics;
use crate::pb::admin_server::{Admin, AdminServer};
use crate::pb::mcp_server::{Mcp, McpServer};
use crate::pb::{JsonRpc, LoadResult, MetricsReply, MetricsRequest, ReloadRequest};
use crate::sidecar::{RpcErr, Sidecar};
use serde_json::{Value, json};
use std::net::IpAddr;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::{RwLock, Semaphore};
use tonic::codegen::tokio_stream::Stream;
use tonic::metadata::MetadataMap;
use tonic::service::Routes;
use tonic::{Code, Request, Response, Status, Streaming};
use tonic_health::ServingStatus;
use tonic_health::pb::health_server::HealthServer;
use tonic_health::server::{HealthReporter, HealthService};

pub const MAX_MESSAGE_BYTES: usize = 4 << 20;
/// Health service names: `""` (overall) and `MCP_SERVICE` follow the load state; `ADMIN_SERVICE` is always serving.
pub const MCP_SERVICE: &str = "ramen.v1.Mcp";
pub const ADMIN_SERVICE: &str = "ramen.v1.Admin";

pub type ConfigSource = Box<dyn Fn() -> Result<Config, String> + Send + Sync>;

pub struct App {
    pub cfg: RwLock<Config>,
    pub sidecar: Arc<Sidecar>,
    pub sem: Semaphore,
    /// `sem`'s capacity, fixed at startup. A later `Admin/Reload` can lower `RAMEN_MAX_INFLIGHT` without
    /// resizing the semaphore, so `inflight` is measured against this and never against the config value.
    pub sem_max: usize,
    /// Whether `grpc::routes` registered server reflection (`RAMEN_REFLECTION`); startup-fixed.
    pub reflection: bool,
    pub metrics: Metrics,
    /// Re-read on `Admin/Reload` (env + yaml in production; injectable for tests).
    pub config_source: ConfigSource,
    pub health: HealthReporter,
}

pub type Shared = Arc<App>;

pub async fn app(cfg: Config) -> Shared {
    app_with(cfg, Box::new(Config::load)).await
}

pub async fn app_with(cfg: Config, config_source: ConfigSource) -> Shared {
    let health = HealthReporter::new();
    for (name, st) in [
        ("", ServingStatus::NotServing),
        (MCP_SERVICE, ServingStatus::NotServing),
        (ADMIN_SERVICE, ServingStatus::Serving),
    ] {
        health.set_service_status(name, st).await;
    }
    let sidecar = Sidecar::new(cfg.clone());
    Arc::new(App {
        sem: Semaphore::new(cfg.max_inflight),
        sem_max: cfg.max_inflight,
        reflection: cfg.reflection,
        cfg: RwLock::new(cfg),
        sidecar,
        metrics: Metrics::default(),
        config_source,
        health,
    })
}

impl App {
    /// `runtime.load`; flips health to SERVING on success.
    pub async fn load(&self) -> Result<Value, RpcErr> {
        let v = self.sidecar.load().await?;
        for name in ["", MCP_SERVICE] {
            self.health
                .set_service_status(name, ServingStatus::Serving)
                .await;
        }
        Ok(v)
    }

    /// Same JSON as the former `GET /metrics`.
    pub async fn metrics_json(&self) -> Value {
        let max = self.cfg.read().await.max_inflight;
        let loaded = self.sidecar.loaded().await;
        let packages = loaded.as_ref().map(|l| json!({"tools": len(&l.result, "tools"), "resources": len(&l.result, "resources"), "prompts": len(&l.result, "prompts"), "errors": len(&l.result, "errors")})).unwrap_or(Value::Null);
        self.metrics.snapshot(
            // Saturating: an `Admin/Reload` that lowers `RAMEN_MAX_INFLIGHT` leaves more permits available
            // than the new `max`, and the semaphore keeps its startup capacity until the pod restarts.
            self.sem_max.saturating_sub(self.sem.available_permits()),
            max,
            self.sidecar.alive().await,
            loaded.map(|l| l.at),
            packages,
        )
    }
}

/// Health, Mcp, Admin (message limit applied), plus server reflection (v1 + v1alpha) when
/// `RAMEN_REFLECTION` is on (the default) so `grpcurl` works without `-proto`.
///
/// Reflection, like Health, runs ahead of every guard: no key, no CIDR check. That is fine on a laptop and
/// is why the default is on, but it lets anyone who can reach the port enumerate the services (including
/// `ramen.v1.Admin`), so a deployment that routes the port through a load balancer sets `RAMEN_REFLECTION=0`
/// — the worker chart and the console's renderers do. With it off both reflection services are not
/// registered at all and answer `UNIMPLEMENTED`.
pub fn routes(app: &Shared) -> Routes {
    let mut routes = Routes::new(HealthServer::new(HealthService::from_health_reporter(
        app.health.clone(),
    )));
    if app.reflection {
        let reflection = || {
            tonic_reflection::server::Builder::configure()
                .register_encoded_file_descriptor_set(crate::pb::FILE_DESCRIPTOR_SET)
                .register_encoded_file_descriptor_set(tonic_health::pb::FILE_DESCRIPTOR_SET)
        };
        routes = routes
            .add_service(reflection().build_v1().expect("reflection v1"))
            .add_service(reflection().build_v1alpha().expect("reflection v1alpha"));
    }
    routes
        .add_service(
            McpServer::new(McpSvc(app.clone())).max_decoding_message_size(MAX_MESSAGE_BYTES),
        )
        .add_service(
            AdminServer::new(AdminSvc(app.clone())).max_decoding_message_size(MAX_MESSAGE_BYTES),
        )
}

fn len(v: &Value, k: &str) -> usize {
    v[k].as_array().map_or(0, Vec::len)
}

fn code_name(c: Code) -> String {
    format!("{c:?}").to_ascii_uppercase()
}

/// One access-log line per call (denied ones included) with `grpc_code`.
struct CallLog {
    ip: IpAddr,
    method: Option<String>,
    name: Option<String>,
    key_id: Option<String>,
    t0: Instant,
}

impl CallLog {
    fn emit(&self, cfg: &Config, status: &str, code: Code, extra: Value) {
        let mut line = json!({"ip": self.ip, "group": cfg.group, "zone": cfg.zone, "env": cfg.env, "method": self.method, "name": self.name,
                              "status": status, "grpc_code": code_name(code), "ms": self.t0.elapsed().as_millis() as u64, "key_id": self.key_id});
        if let Value::Object(m) = extra {
            line.as_object_mut().unwrap().extend(m);
        }
        emit("info", "mcp", line);
    }
    fn deny(&self, cfg: &Config, st: Status) -> Status {
        self.emit(cfg, "denied", st.code(), Value::Null);
        st
    }
}

fn rpc_error(id: Value, code: i64, msg: &str) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": msg}})
}

fn reply(v: Value) -> Response<JsonRpc> {
    Response::new(JsonRpc {
        body: v.to_string().into_bytes(),
    })
}

pub struct McpSvc(pub Shared);

#[tonic::async_trait]
impl Mcp for McpSvc {
    type SessionStream = Pin<Box<dyn Stream<Item = Result<JsonRpc, Status>> + Send>>;

    async fn call(&self, req: Request<JsonRpc>) -> Result<Response<JsonRpc>, Status> {
        let app = &self.0;
        let cfg = app.cfg.read().await.clone();
        let md: &MetadataMap = req.metadata();
        let mut log = CallLog {
            ip: auth::client_ip(&cfg, req.remote_addr(), md),
            method: None,
            name: None,
            key_id: None,
            t0: Instant::now(),
        };
        if !auth::ip_allowed(&cfg, log.ip) {
            return Err(log.deny(&cfg, Status::permission_denied("ip not allowed")));
        }
        let Some(key_id) = auth::check_key(&cfg, md) else {
            return Err(log.deny(&cfg, Status::unauthenticated("unauthorized")));
        };
        log.key_id = Some(key_id);
        let body = req.into_inner().body;
        let Ok(rpc) = serde_json::from_slice::<Value>(&body) else {
            log.emit(&cfg, "error", Code::Ok, json!({"parse": "invalid json"}));
            return Ok(reply(rpc_error(Value::Null, -32700, "parse error")));
        };
        let id = rpc.get("id").cloned().unwrap_or(Value::Null);
        let method = rpc["method"].as_str().unwrap_or("").to_string();
        if method.is_empty() {
            log.emit(&cfg, "error", Code::Ok, Value::Null);
            return Ok(reply(rpc_error(
                id,
                -32600,
                "invalid request: method missing",
            )));
        }
        let params = rpc.get("params").cloned().unwrap_or(json!({}));
        log.method = Some(method.clone());
        log.name = mcp::target_name(&method, &params);
        let Ok(_permit) = app.sem.try_acquire() else {
            app.metrics.record(false);
            return Err(log.deny(
                &cfg,
                Status::resource_exhausted("busy: max inflight reached"),
            ));
        };
        let out = mcp::dispatch(&app.sidecar, &cfg.blocked, &method, &params).await;
        let ok = match &out {
            Ok(Some(r)) => !r.get("isError").and_then(Value::as_bool).unwrap_or(false),
            Ok(None) => true,
            Err(_) => false,
        };
        app.metrics.record(ok);
        let extra = if cfg.verbose {
            json!({"request": rpc, "response": match &out {
                Ok(v) => v.clone().unwrap_or(Value::Null),
                Err(e) => json!({"code": e.code, "message": e.message}),
            }})
        } else {
            Value::Null
        };
        log.emit(&cfg, if ok { "ok" } else { "error" }, Code::Ok, extra);
        Ok(match out {
            Ok(Some(result)) => reply(json!({"jsonrpc": "2.0", "id": id, "result": result})),
            Ok(None) => Response::new(JsonRpc::default()),
            Err(RpcErr { code, message }) => reply(rpc_error(id, code, &message)),
        })
    }

    async fn session(
        &self,
        _req: Request<Streaming<JsonRpc>>,
    ) -> Result<Response<Self::SessionStream>, Status> {
        Err(Status::unimplemented(
            "Mcp/Session is reserved; use unary Mcp/Call",
        ))
    }
}

pub struct AdminSvc(pub Shared);

impl AdminSvc {
    async fn guard<T>(&self, req: &Request<T>) -> Result<Config, Status> {
        let cfg = self.0.cfg.read().await.clone();
        let md = req.metadata();
        if !auth::admin_ip_allowed(&cfg, auth::client_ip(&cfg, req.remote_addr(), md)) {
            return Err(Status::permission_denied("ip not allowed"));
        }
        if !auth::check_admin(&cfg, md) {
            return Err(Status::unauthenticated("admin key required"));
        }
        Ok(cfg)
    }
}

#[tonic::async_trait]
impl Admin for AdminSvc {
    async fn reload(&self, req: Request<ReloadRequest>) -> Result<Response<LoadResult>, Status> {
        self.guard(&req).await?;
        let app = &self.0;
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
        let v = app.load().await.map_err(|e| Status::internal(e.message))?;
        emit(
            "info",
            "reloaded",
            json!({"tools": len(&v, "tools"), "errors": len(&v, "errors")}),
        );
        Ok(Response::new(LoadResult {
            json: v.to_string().into_bytes(),
        }))
    }

    async fn metrics(
        &self,
        req: Request<MetricsRequest>,
    ) -> Result<Response<MetricsReply>, Status> {
        self.guard(&req).await?;
        Ok(Response::new(MetricsReply {
            json: self.0.metrics_json().await.to_string().into_bytes(),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn code_names_are_grpc_style() {
        assert_eq!(code_name(Code::Ok), "OK");
        assert_eq!(code_name(Code::PermissionDenied), "PERMISSIONDENIED");
    }
}
