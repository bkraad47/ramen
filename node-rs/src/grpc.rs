//! gRPC services (CONTRACTS §11): `ramen.v1.Mcp/Call` (JSON-RPC bytes in/out), `ramen.v1.Admin/{Reload,Metrics}`,
//! `grpc.health.v1.Health` (NOT_SERVING until the first successful `runtime.load`). Guards run inside the
//! handlers so they see the peer address and metadata: CIDR → PERMISSION_DENIED, key → UNAUTHENTICATED,
//! inflight → RESOURCE_EXHAUSTED; the 4 MiB message limit is enforced by the codec (OUT_OF_RANGE).
//!
//! §16.1: `guard` and `dispatch_body` are the ONE implementation of those checks. The Streamable HTTP handler in
//! `http.rs` calls the same two functions with the request headers as a `MetadataMap`; the transports differ only in
//! how a `Status` is spelled on the wire (`http::status_of`). Never add a check here without it applying there.
use crate::auth;
use crate::config::Config;
use crate::log::emit;
use crate::mcp;
use crate::metrics::Metrics;
use crate::pb::admin_server::{Admin, AdminServer};
use crate::pb::mcp_server::{Mcp, McpServer};
use crate::pb::{JsonRpc, LoadResult, MetricsReply, MetricsRequest, ReloadRequest};
use crate::session::{self, Sessions};
use crate::sidecar::{RpcErr, Sidecar};
use crate::token;
use serde_json::{Value, json};
use std::net::{IpAddr, SocketAddr};
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
    /// §16.2: signs and verifies `Mcp-Session-Id`s; startup-fixed like the semaphore.
    pub sessions: Sessions,
    /// §16.3: HS256 key for console-issued access tokens, derived from the same secret.
    pub token_key: ring::hmac::Key,
    /// N7: item/scope rate limiting. Redis connections are startup-fixed like `sessions`/`sem_max`.
    pub throttle: crate::throttle::Throttle,
    /// C11: sessions this pod has told about the current manifest (`<nonce>.<hash12>`), bounded at 4096.
    pub notified: session::Notified,
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
    let sessions = Sessions::new(cfg.session_secret.as_deref(), cfg.session_ttl_secs);
    if sessions.generated {
        emit(
            "warn",
            "RAMEN_SESSION_SECRET unset: session ids and OAuth tokens are per pod and die with it",
            json!({}),
        );
    }
    let token_key = sessions.derived_key("oauth");
    let throttle = crate::throttle::Throttle::connect(&cfg).await;
    Arc::new(App {
        sem: Semaphore::new(cfg.max_inflight),
        sem_max: cfg.max_inflight,
        reflection: cfg.reflection,
        cfg: RwLock::new(cfg),
        sidecar,
        metrics: Metrics::default(),
        config_source,
        health,
        sessions,
        token_key,
        throttle,
        notified: session::Notified::new(4096),
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
        // C2: the runtime's manifest hash (C1), kept with the last load result; "" before the first load
        let hash = loaded
            .as_ref()
            .and_then(|l| l.result["hash"].as_str())
            .unwrap_or("")
            .to_string();
        self.metrics.snapshot(
            // Saturating: an `Admin/Reload` that lowers `RAMEN_MAX_INFLIGHT` leaves more permits available
            // than the new `max`, and the semaphore keeps its startup capacity until the pod restarts.
            self.sem_max.saturating_sub(self.sem.available_permits()),
            max,
            self.sidecar.alive().await,
            loaded.map(|l| l.at),
            packages,
            &hash,
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

/// One access-log line per call (denied ones included) with `grpc_code`, on either transport.
pub struct CallLog {
    pub ip: IpAddr,
    pub method: Option<String>,
    pub name: Option<String>,
    /// The key id for an `rmk_` key, `user:<sub>` for a console-issued token — never the credential itself.
    pub key_id: Option<String>,
    pub transport: &'static str,
    t0: Instant,
}

impl CallLog {
    pub fn new(ip: IpAddr, transport: &'static str) -> Self {
        Self {
            ip,
            method: None,
            name: None,
            key_id: None,
            transport,
            t0: Instant::now(),
        }
    }
    pub fn emit(&self, cfg: &Config, status: &str, code: Code, extra: Value) {
        // N4: the line's own `level` must match its `status` — a denied/failed call is not "info", or a
        // level-based filter (alerting, external log monitors) silently misses every one of them.
        let level = match status {
            "ok" => "info",
            "denied" => "warn",
            _ => "error",
        };
        let mut line = json!({"ip": self.ip, "group": cfg.group, "zone": cfg.zone, "env": cfg.env, "method": self.method, "name": self.name,
                              "status": status, "grpc_code": code_name(code), "ms": self.t0.elapsed().as_millis() as u64, "key_id": self.key_id,
                              "transport": self.transport});
        if let Value::Object(m) = extra {
            line.as_object_mut().unwrap().extend(m);
        }
        emit(level, "mcp", line);
    }
    /// C6 (0.7.0): every denial names its `reason` (`no_key`, `bad_key`, `token_expired`, `token_invalid`,
    /// `cidr`, `blocked_name`, `throttled`, `origin`, `too_large`, `inflight`, `session_expired`,
    /// `session_invalid`) — a short code, never the credential.
    pub fn deny(&self, cfg: &Config, st: Status, reason: &'static str) -> Status {
        self.emit(cfg, "denied", st.code(), json!({"reason": reason}));
        st
    }
}

/// Who a call is from, once the credential checked out.
#[derive(Debug, Clone)]
pub struct Caller {
    /// What the access log names: the short key id, or `user:<sub>` for a token.
    pub id: String,
    /// What a session id is bound to (§16.2): a full SHA-256 of the key — the log id is a 32-bit hash and two
    /// keys could share one (security review 0.5.0 L3) — or `user:<sub>` for a token.
    pub binding: String,
    /// Present for a console-issued token (§16.3).
    pub principal: Option<token::Principal>,
    /// C10: what `RAMEN_TOOL_ACCESS` keys on — `key` for an `rmk_` key, the token's `role` claim otherwise
    /// (`mcp_user` when the claim is missing).
    pub kind: String,
}

/// C12: a 12-hex sha256 prefix of the canonical JSON (sorted keys, no spaces — serde_json's own `to_string`) of a
/// call's `arguments`, `""` when absent. The console's drift detection groups by it; the arguments are never logged.
pub fn args_digest(arguments: Option<&Value>) -> String {
    let Some(v) = arguments else {
        return String::new();
    };
    let d = ring::digest::digest(&ring::digest::SHA256, v.to_string().as_bytes());
    d.as_ref()[..6].iter().map(|b| format!("{b:02x}")).collect()
}

/// The credential check shared by both transports: an `rmk_` key (constant-time, §11) or, when `RAMEN_OAUTH_ISSUER`
/// is set, a console-issued HS256 token for this group and zone (§16.3). A token is only tried when the bearer has
/// the three-part JWT shape, so key comparison stays the first and constant-time path. `Err` is the access-log
/// reason (C6); the caller only ever sees "unauthorized".
pub fn credential(app: &App, cfg: &Config, md: &MetadataMap) -> Result<Caller, &'static str> {
    if let Some(id) = auth::check_key(cfg, md) {
        let raw = auth::bearer(md).unwrap_or("");
        let digest = ring::digest::digest(&ring::digest::SHA256, raw.as_bytes());
        let binding = digest.as_ref().iter().map(|b| format!("{b:02x}")).collect();
        return Ok(Caller {
            id,
            binding,
            principal: None,
            kind: "key".into(),
        });
    }
    let bearer = auth::bearer(md).ok_or("no_key")?;
    let issuer = cfg.oauth_issuer.as_deref().ok_or("bad_key")?;
    if bearer.matches('.').count() != 2 {
        return Err("bad_key");
    }
    let p = token::verify(
        bearer,
        &app.token_key,
        issuer,
        &cfg.group,
        &cfg.zone,
        session::now(),
    )
    .map_err(|e| match e {
        token::Reject::Expired => "token_expired",
        _ => "token_invalid",
    })?;
    // A2: the binding is the person (`sub`), not the token — a refreshed token keeps the session.
    let id = format!("user:{}", p.sub);
    Ok(Caller {
        binding: id.clone(),
        id,
        kind: p.role.clone().unwrap_or_else(|| "mcp_user".into()),
        principal: Some(p),
    })
}

/// Address allowlist, then credential — the order every call goes through on either transport.
pub fn guard(
    app: &App,
    cfg: &Config,
    peer: Option<SocketAddr>,
    md: &MetadataMap,
    transport: &'static str,
) -> Result<(CallLog, Caller), Status> {
    let mut log = CallLog {
        ip: auth::client_ip(cfg, peer, md),
        method: None,
        name: None,
        key_id: None,
        transport,
        t0: Instant::now(),
    };
    if !auth::ip_allowed(cfg, log.ip) {
        return Err(log.deny(cfg, Status::permission_denied("ip not allowed"), "cidr"));
    }
    let caller = match credential(app, cfg, md) {
        Ok(c) => c,
        Err(reason) => {
            return Err(log.deny(cfg, Status::unauthenticated("unauthorized"), reason));
        }
    };
    log.key_id = Some(caller.id.clone());
    Ok((log, caller))
}

/// Parse one JSON-RPC message, take an inflight permit, dispatch, and log. `Ok(None)` is a notification.
/// JSON-RPC protocol errors come back as `Ok(Some(error body))`, never as a transport error (§11).
/// `kind` is the caller's kind (`Caller::kind`) for the C10 tool-access policy.
pub async fn dispatch_body(
    app: &App,
    cfg: &Config,
    log: &mut CallLog,
    kind: &str,
    body: &[u8],
) -> Result<Option<Value>, Status> {
    let Ok(rpc) = serde_json::from_slice::<Value>(body) else {
        log.emit(cfg, "error", Code::Ok, json!({"parse": "invalid json"}));
        return Ok(Some(rpc_error(Value::Null, -32700, "parse error")));
    };
    let id = rpc.get("id").cloned().unwrap_or(Value::Null);
    let method = rpc["method"].as_str().unwrap_or("").to_string();
    if method.is_empty() {
        log.emit(cfg, "error", Code::Ok, Value::Null);
        return Ok(Some(rpc_error(
            id,
            -32600,
            "invalid request: method missing",
        )));
    }
    let params = rpc.get("params").cloned().unwrap_or(json!({}));
    log.method = Some(method.clone());
    log.name = mcp::target_name(&method, &params);
    if !app
        .throttle
        .allow(cfg, log.ip, log.key_id.as_deref(), log.name.as_deref())
        .await
    {
        app.metrics.record(false);
        return Err(log.deny(
            cfg,
            Status::resource_exhausted("throttled: rate limit exceeded"),
            "throttled",
        ));
    }
    let Ok(_permit) = app.sem.try_acquire() else {
        app.metrics.record(false);
        return Err(log.deny(
            cfg,
            Status::resource_exhausted("busy: max inflight reached"),
            "inflight",
        ));
    };
    // C6: a call to a blocked name is a denial in the log (`blocked_name`); on the wire it stays the
    // `-32601` "not found" that `mcp::dispatch` answers (§9), so a caller learns nothing new.
    let blocked = mcp::blocked_target(&app.sidecar, &cfg.blocked, &method, &params).await;
    let policy = mcp::Policy {
        blocked: &cfg.blocked,
        tool_access: &cfg.tool_access,
        kind,
    };
    // C10: `tool_hidden` / `tool_denied` — like `blocked_name`, the wire answer comes from `mcp::dispatch`.
    let access_denial = policy.denial(&method, &params);
    let out = mcp::dispatch(&app.sidecar, &policy, &method, &params).await;
    let ok = match &out {
        Ok(Some(r)) => !r.get("isError").and_then(Value::as_bool).unwrap_or(false),
        Ok(None) => true,
        Err(_) => false,
    };
    app.metrics.record(ok);
    let mut extra = if cfg.verbose {
        json!({"request": rpc, "response": match &out {
            Ok(v) => v.clone().unwrap_or(Value::Null),
            Err(e) => json!({"code": e.code, "message": e.message}),
        }})
    } else {
        json!({})
    };
    if method == "tools/call" {
        extra["args"] = json!(args_digest(params.get("arguments"))); // C12
    }
    let status = if blocked {
        extra["reason"] = json!("blocked_name");
        "denied"
    } else if let Some(reason) = access_denial {
        extra["reason"] = json!(reason);
        "denied"
    } else if ok {
        "ok"
    } else {
        "error"
    };
    log.emit(cfg, status, Code::Ok, extra);
    Ok(match out {
        Ok(Some(result)) => Some(json!({"jsonrpc": "2.0", "id": id, "result": result})),
        Ok(None) => None,
        Err(RpcErr { code, message }) => Some(rpc_error(id, code, &message)),
    })
}

pub fn rpc_error(id: Value, code: i64, msg: &str) -> Value {
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
        let (mut log, caller) = guard(app, &cfg, req.remote_addr(), req.metadata(), "grpc")?;
        let body = req.into_inner().body;
        Ok(
            match dispatch_body(app, &cfg, &mut log, &caller.kind, &body).await? {
                Some(v) => reply(v),
                None => Response::new(JsonRpc::default()),
            },
        )
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

    fn cfg(pairs: &[(&str, &str)]) -> Config {
        Config::from_map(
            &pairs
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect::<std::collections::HashMap<_, _>>(),
        )
        .unwrap()
    }

    #[test]
    fn call_log_level_matches_status() {
        // Holding TEST_LOCK only keeps another test from redirecting FILE out from under this one (the
        // documented v0.5.5 flake); it does not stop another thread's own emit() from also mirroring into
        // whatever path FILE currently points at. So each line carries a nonce and is found by that, not by
        // file position — any interleaved line from an unrelated concurrent test is just ignored.
        let _guard = crate::log::TEST_LOCK
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let p = std::env::temp_dir().join(format!("ramen-grpc-log-{}.log", std::process::id()));
        crate::log::set_file(Some(p.clone()));
        let c = cfg(&[]);
        for (status, want_level) in [("ok", "info"), ("denied", "warn"), ("error", "error")] {
            let nonce = format!("clm-{status}-{:?}", std::time::Instant::now());
            CallLog::new("127.0.0.1".parse().unwrap(), "grpc").emit(
                &c,
                status,
                Code::Ok,
                json!({"nonce": nonce}),
            );
            let text = std::fs::read_to_string(&p).unwrap();
            let line = text
                .lines()
                .rev()
                .filter_map(|l| serde_json::from_str::<Value>(l).ok())
                .find(|v| v["nonce"] == nonce)
                .unwrap_or_else(|| panic!("no emitted line carried nonce {nonce}"));
            assert_eq!(line["status"], status);
            assert_eq!(
                line["level"], want_level,
                "status {status} must log at level {want_level}"
            );
        }
        crate::log::set_file(None);
        let _ = std::fs::remove_file(&p);
    }

    fn h(pairs: &[(&'static str, &str)]) -> MetadataMap {
        let mut m = MetadataMap::new();
        for (k, v) in pairs {
            m.insert(*k, v.parse().unwrap());
        }
        m
    }

    fn user_token(secret: &str, claims: Value) -> String {
        let key = crate::session::Sessions::new(Some(secret), 60).derived_key("oauth");
        format!("Bearer {}", token::mint(&claims, &key))
    }

    /// C6: `credential` names why it refused (for the access log only; the wire says "unauthorized").
    #[tokio::test]
    async fn credential_names_the_reason_it_refuses() {
        let c = cfg(&[
            ("RAMEN_MCP_KEYS", "rmk_test"),
            ("RAMEN_SESSION_SECRET", "s"),
            ("RAMEN_OAUTH_ISSUER", "https://console.example"),
            ("RAMEN_GROUP", "demo"),
            ("RAMEN_ZONE", "a"),
        ]);
        let a = app(c.clone()).await;
        let now = session::now();
        let claims = |exp: u64, aud: &str| json!({"iss": "https://console.example", "sub": "u1", "aud": aud, "scope": aud, "exp": exp});
        let reason = |md: MetadataMap| credential(&a, &c, &md).err();
        assert_eq!(reason(h(&[])), Some("no_key"));
        assert_eq!(
            reason(h(&[("authorization", "Basic rmk_test")])),
            Some("no_key")
        );
        assert_eq!(
            reason(h(&[("authorization", "Bearer rmk_wrong")])),
            Some("bad_key")
        );
        assert_eq!(
            reason(h(&[("authorization", "Bearer a.b.c")])),
            Some("token_invalid")
        );
        assert_eq!(
            reason(h(&[(
                "authorization",
                &user_token("s", claims(now - 1, "mcp:demo:a"))
            )])),
            Some("token_expired")
        );
        assert_eq!(
            reason(h(&[(
                "authorization",
                &user_token("s", claims(now + 60, "mcp:demo:b"))
            )])),
            Some("token_invalid")
        );
        assert_eq!(
            reason(h(&[(
                "authorization",
                &user_token("other", claims(now + 60, "mcp:demo:a"))
            )])),
            Some("token_invalid")
        );
        let ok = credential(
            &a,
            &c,
            &h(&[(
                "authorization",
                &user_token("s", claims(now + 60, "mcp:demo:a")),
            )]),
        )
        .unwrap();
        assert_eq!(
            (ok.id.as_str(), ok.binding.as_str(), ok.kind.as_str()),
            ("user:u1", "user:u1", "mcp_user") // C10: no `role` claim → mcp_user
        );
        let mut with_role = claims(now + 60, "mcp:demo:a");
        with_role["role"] = json!("super_admin");
        assert_eq!(
            credential(
                &a,
                &c,
                &h(&[("authorization", &user_token("s", with_role))])
            )
            .unwrap()
            .kind,
            "super_admin"
        );
        let by_key = credential(&a, &c, &h(&[("authorization", "Bearer rmk_test")])).unwrap();
        assert_eq!((by_key.principal, by_key.kind.as_str()), (None, "key"));
        // no issuer: a JWT-shaped bearer is just a wrong key
        let plain = cfg(&[("RAMEN_MCP_KEYS", "rmk_test")]);
        assert_eq!(
            credential(
                &*app(plain.clone()).await,
                &plain,
                &h(&[("authorization", "Bearer a.b.c")])
            )
            .err(),
            Some("bad_key")
        );
    }

    fn last_line_with(p: &std::path::Path, nonce: &str) -> Value {
        let text = std::fs::read_to_string(p).unwrap();
        text.lines()
            .rev()
            .filter_map(|l| serde_json::from_str::<Value>(l).ok())
            .find(|v| v["nonce"] == nonce || v["name"] == nonce)
            .unwrap_or_else(|| panic!("no emitted line carried {nonce}"))
    }

    /// C6: the access-log line of every denial carries `reason` and never the credential. The log-file lock is
    /// taken in sync code and the async work runs under `block_on`, so no guard is held across an `.await`.
    #[test]
    fn denials_carry_a_reason_and_never_the_credential() {
        let _guard = crate::log::TEST_LOCK
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let p = std::env::temp_dir().join(format!("ramen-reason-log-{}.log", std::process::id()));
        crate::log::set_file(Some(p.clone()));
        tokio::runtime::Runtime::new()
            .unwrap()
            .block_on(reason_lines(&p));
        crate::log::set_file(None);
        let _ = std::fs::remove_file(p);
    }

    async fn reason_lines(p: &std::path::Path) {
        let c = cfg(&[("RAMEN_MCP_KEYS", "rmk_test")]);
        let a = app(c.clone()).await;
        // guard: wrong key
        let md = h(&[("authorization", "Bearer rmk_wrong")]);
        let Err(st) = guard(&a, &c, None, &md, "grpc") else {
            panic!("a wrong key must be refused")
        };
        assert_eq!(st.code(), Code::Unauthenticated);
        let text = std::fs::read_to_string(p).unwrap();
        let line = text
            .lines()
            .rev()
            .filter_map(|l| serde_json::from_str::<Value>(l).ok())
            .find(|v| v["reason"] == "bad_key")
            .expect("a denied line with reason bad_key");
        assert_eq!(
            (line["status"].as_str(), line["level"].as_str()),
            (Some("denied"), Some("warn"))
        );
        assert!(
            !text.contains("rmk_wrong"),
            "the credential must never be logged"
        );
        // blocked name: wire stays -32601, log says denied/blocked_name
        let blocked = cfg(&[
            ("RAMEN_MCP_KEYS", "rmk_test"),
            ("RAMEN_BLOCKED", "calc-nonce"),
        ]);
        a.sidecar
            .set_loaded_for_test(
                json!({"tools": [{"name": "calc-nonce", "inputSchema": {"type": "object"}}]}),
            )
            .await;
        let mut log = CallLog::new("127.0.0.1".parse().unwrap(), "grpc");
        let out = dispatch_body(
            &a,
            &blocked,
            &mut log,
            "key",
            br#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"calc-nonce","arguments":{}}}"#,
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(out["error"]["code"], -32601);
        let line = last_line_with(p, "calc-nonce");
        assert_eq!(
            (line["status"].as_str(), line["reason"].as_str()),
            (Some("denied"), Some("blocked_name"))
        );
        // C10: tool access — hidden (-32601, tool_hidden) and listed-but-denied (-32003, tool_denied)
        let mut access = cfg(&[("RAMEN_MCP_KEYS", "rmk_test")]);
        access.tool_access = crate::config::parse_tool_access(
            r#"{"calc-nonce":{"list":["key","viewer"],"call":["key"]}}"#,
        )
        .unwrap();
        let call = |name: &str, args: &str| {
            format!(
                r#"{{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{{"name":"{name}","arguments":{args}}}}}"#
            )
        };
        let mut log = CallLog::new("127.0.0.1".parse().unwrap(), "grpc");
        let out = dispatch_body(
            &a,
            &access,
            &mut log,
            "mcp_user",
            call("calc-nonce", "{}").as_bytes(),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(out["error"]["code"], -32601, "{out}");
        let line = last_line_with(p, "calc-nonce");
        assert_eq!(
            (line["status"].as_str(), line["reason"].as_str()),
            (Some("denied"), Some("tool_hidden"))
        );
        let mut log = CallLog::new("127.0.0.1".parse().unwrap(), "grpc");
        let out = dispatch_body(
            &a,
            &access,
            &mut log,
            "viewer",
            call("calc-nonce", "{}").as_bytes(),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(
            (
                out["error"]["code"].as_i64(),
                out["error"]["message"].as_str()
            ),
            (
                Some(-32003),
                Some("forbidden: calc-nonce is not callable for viewer")
            )
        );
        let line = last_line_with(p, "calc-nonce");
        assert_eq!(
            (line["status"].as_str(), line["reason"].as_str()),
            (Some("denied"), Some("tool_denied"))
        );
        // C12: `args` is a 12-hex digest of the canonical arguments, equal for equal args in any key order,
        // "" when absent, and the arguments themselves never appear in the line
        let mut log = CallLog::new("127.0.0.1".parse().unwrap(), "grpc");
        let _ = dispatch_body(
            &a,
            &access,
            &mut log,
            "key",
            call("calc-nonce", r#"{"b":"secret-arg-value","a":1}"#).as_bytes(),
        )
        .await;
        let first = last_line_with(p, "calc-nonce")["args"]
            .as_str()
            .unwrap()
            .to_string();
        assert_eq!(first.len(), 12);
        assert!(first.bytes().all(|b| b.is_ascii_hexdigit()), "{first}");
        assert_eq!(
            first,
            crate::grpc::args_digest(Some(&json!({"a": 1, "b": "secret-arg-value"})))
        );
        let mut log = CallLog::new("127.0.0.1".parse().unwrap(), "grpc");
        let _ = dispatch_body(
            &a,
            &access,
            &mut log,
            "key",
            call("calc-nonce", r#"{"a":1,"b":"secret-arg-value"}"#).as_bytes(),
        )
        .await;
        assert_eq!(last_line_with(p, "calc-nonce")["args"], first);
        let mut log = CallLog::new("127.0.0.1".parse().unwrap(), "grpc");
        let _ = dispatch_body(
            &a,
            &access,
            &mut log,
            "key",
            call("calc-nonce", r#"{"a":2,"b":"secret-arg-value"}"#).as_bytes(),
        )
        .await;
        assert_ne!(last_line_with(p, "calc-nonce")["args"], first);
        let mut log = CallLog::new("127.0.0.1".parse().unwrap(), "grpc");
        let _ = dispatch_body(
            &a,
            &access,
            &mut log,
            "key",
            br#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"calc-nonce"}}"#,
        )
        .await;
        assert_eq!(last_line_with(p, "calc-nonce")["args"], "");
        let mut log = CallLog::new("127.0.0.1".parse().unwrap(), "grpc");
        let _ = dispatch_body(
            &a,
            &access,
            &mut log,
            "key",
            br#"{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"name":"calc-nonce"}}"#,
        )
        .await;
        let listed = std::fs::read_to_string(p)
            .unwrap()
            .lines()
            .rev()
            .filter_map(|l| serde_json::from_str::<Value>(l).ok())
            .find(|v| v["method"] == "tools/list")
            .unwrap();
        assert!(listed.get("args").is_none(), "args only on tools/call");
        assert!(
            !std::fs::read_to_string(p)
                .unwrap()
                .contains("secret-arg-value")
        );
        // inflight: every permit taken
        let permits = a.sem.try_acquire_many(a.sem_max as u32).unwrap();
        let mut log = CallLog::new("127.0.0.1".parse().unwrap(), "grpc");
        let st = dispatch_body(
            &a,
            &c,
            &mut log,
            "key",
            br#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"busy-nonce"}}"#,
        )
        .await
        .unwrap_err();
        drop(permits);
        assert_eq!(st.code(), Code::ResourceExhausted);
        assert_eq!(last_line_with(p, "busy-nonce")["reason"], "inflight");
        // the plain deny path keeps the reason verbatim (cidr/origin/too_large/session_* come through here)
        for reason in [
            "cidr",
            "origin",
            "too_large",
            "session_expired",
            "session_invalid",
            "throttled",
        ] {
            let mut log = CallLog::new("127.0.0.1".parse().unwrap(), "http");
            log.name = Some(format!("deny-{reason}"));
            log.deny(&c, Status::permission_denied("x"), reason);
            assert_eq!(
                last_line_with(p, &format!("deny-{reason}"))["reason"],
                reason
            );
        }
        crate::log::set_file(None);
        let _ = std::fs::remove_file(p);
    }

    /// C2: `Admin/Metrics` reports the manifest hash of the last load, `""` before it.
    #[tokio::test]
    async fn metrics_report_the_manifest_hash() {
        let a = app(cfg(&[])).await;
        assert_eq!(a.metrics_json().await["manifest_hash"], "");
        a.sidecar
            .set_loaded_for_test(json!({"tools": [], "hash": "deadbeef"}))
            .await;
        assert_eq!(a.metrics_json().await["manifest_hash"], "deadbeef");
    }
}
