//! gRPC services (CONTRACTS §11): `ramen.v1.Mcp/Call` (JSON-RPC bytes in/out), `ramen.v1.Admin/{Reload,Metrics}`,
//! `grpc.health.v1.Health` (NOT_SERVING until the first successful `runtime.load`). Guards run inside the
//! handlers so they see the peer address and metadata: CIDR → PERMISSION_DENIED, key → UNAUTHENTICATED,
//! inflight → RESOURCE_EXHAUSTED; the 4 MiB message limit is enforced by the codec (OUT_OF_RANGE).
//!
//! §16.1: `guard` and `dispatch_body` are the ONE implementation of those checks. The Streamable HTTP handler in
//! `http.rs` calls the same two functions with the request headers as a `MetadataMap`; the transports differ only in
//! how a `Status` is spelled on the wire (`http::status_of`). Never add a check here without it applying there.
//!
//! §23 (1.0.0): `dispatch_call` is that one implementation for both eras — 2025-06-18 exactly as before, and
//! 2026-07-28 (`modern.rs`) selected per message. `dispatch_body` is the same function behind its pre-1.0 signature.
use crate::auth;
use crate::config::Config;
use crate::log::emit;
use crate::mcp;
use crate::metrics::Metrics;
use crate::modern;
use crate::pb::admin_server::{Admin, AdminServer};
use crate::pb::mcp_server::{Mcp, McpServer};
use crate::pb::{JsonRpc, LoadResult, MetricsReply, MetricsRequest, ReloadRequest};
use crate::session::{self, Sessions};
use crate::sidecar::{ProgressFn, RpcErr, Sidecar};
use crate::subs::{Subs, Want};
use crate::tasks::{self, Tasks};
use crate::token;
use http::HeaderMap;
use serde_json::{Value, json};
use std::net::{IpAddr, SocketAddr};
use std::pin::Pin;
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::{RwLock, Semaphore, mpsc};
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
    /// §23.4: signs `requestState`; derived from the session secret like `token_key` (label `mrtr`).
    pub mrtr_key: ring::hmac::Key,
    /// §23.5: task records (scope Redis, else item Redis, else memory) and the tasks this pod runs.
    pub tasks: Arc<Tasks>,
    /// §23.6: open `subscriptions/listen` streams.
    pub subs: Arc<Subs>,
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
    let mrtr_key = sessions.derived_key("mrtr");
    let throttle = crate::throttle::Throttle::connect(&cfg).await;
    let store = match throttle.shared_store() {
        Some(conn) => tasks::Store::Redis(conn),
        None => tasks::Store::memory(),
    };
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
        mrtr_key,
        tasks: Tasks::new(store),
        subs: Subs::new(),
    })
}

impl App {
    /// `runtime.load`; flips health to SERVING on success. §23.6: a load that changes the manifest hash is told to
    /// every open subscription stream (the first load of a pod is not a change).
    pub async fn load(&self) -> Result<Value, RpcErr> {
        let hash_of = |v: &Value| v["hash"].as_str().map(String::from);
        let before = self.sidecar.loaded().await.map(|l| hash_of(&l.result));
        let v = self.sidecar.load().await?;
        if before.is_some_and(|b| b != hash_of(&v)) {
            self.subs.list_changed();
        }
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
        // §21.2: the runtime's guardrails block rides with the load result; "none" before the first load
        let guardrails = loaded
            .as_ref()
            .and_then(|l| l.result.get("guardrails").cloned())
            .unwrap_or_else(|| json!({"engine": "none", "tools": {}}));
        let mut m = self.metrics.snapshot(
            // Saturating: an `Admin/Reload` that lowers `RAMEN_MAX_INFLIGHT` leaves more permits available
            // than the new `max`, and the semaphore keeps its startup capacity until the pod restarts.
            self.sem_max.saturating_sub(self.sem.available_permits()),
            max,
            self.sidecar.alive().await,
            loaded.map(|l| l.at),
            packages,
            &hash,
            guardrails,
        );
        // §23.1: open streams and live tasks on this pod
        m["subscriptions"] = json!(self.subs.open());
        m["tasks"] = json!(self.tasks.live());
        m
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
    /// §23.1: the protocol version this call is served as; `null` on a line written before the body was read.
    pub protocol: Option<String>,
    /// §23.2: W3C trace context of a modern call, for this line only.
    pub trace: Option<Value>,
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
            protocol: None,
            trace: None,
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
                              "transport": self.transport, "protocol": self.protocol});
        if let Some(t) = &self.trace {
            line["trace"] = t.clone();
        }
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
    let mut log = CallLog::new(auth::client_ip(cfg, peer, md), transport);
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

/// What `dispatch_call` needs to know about the caller and the transport beyond the access-log record.
pub struct Ctx<'a> {
    /// The caller's kind (`Caller::kind`) for the C10 tool-access policy.
    pub kind: &'a str,
    /// §16.2 binding: what a `requestState` (§23.4) and a task (§23.5) are tied to.
    pub binding: &'a str,
    /// The HTTP request headers; `None` on gRPC, where the header rules of §23.2 do not apply and the body alone
    /// selects the era.
    pub http: Option<&'a HeaderMap>,
    /// §23.4: where `notifications/progress` messages go while the call runs. `Some` only when the transport can
    /// carry them (an HTTP client that accepts `text/event-stream`).
    pub progress: Option<mpsc::UnboundedSender<Value>>,
}

/// The answer to one message.
pub struct Reply {
    /// `None` for a notification (HTTP 202, an empty gRPC body).
    pub body: Option<Value>,
    /// The HTTP status the body travels with: always 200 on 2025-06-18; §23.1 for a modern JSON-RPC error.
    pub status: u16,
    /// Served as 2026-07-28: no session id is read, minted or echoed.
    pub modern: bool,
    /// §23.6: the frames of a `subscriptions/listen` stream, the ack already in it.
    pub listen: Option<mpsc::UnboundedReceiver<String>>,
}

impl Reply {
    fn legacy(body: Option<Value>) -> Self {
        Reply {
            body,
            status: 200,
            modern: false,
            listen: None,
        }
    }
    fn modern(body: Option<Value>, status: u16) -> Self {
        Reply {
            body,
            status,
            modern: true,
            listen: None,
        }
    }
}

/// `dispatch_call` for a 2025-06-18 caller with no transport context — the signature every caller had before
/// 1.0.0. A modern body is still served as modern (the body selects the era); its binding is then empty.
pub async fn dispatch_body(
    app: &App,
    cfg: &Config,
    log: &mut CallLog,
    kind: &str,
    body: &[u8],
) -> Result<Option<Value>, Status> {
    let ctx = Ctx {
        kind,
        binding: "",
        http: None,
        progress: None,
    };
    Ok(dispatch_call(app, cfg, log, ctx, body).await?.body)
}

/// A modern check failed (§23.1): log it, answer it with the request id.
fn refuse(cfg: &Config, log: &CallLog, id: Value, r: modern::Refusal) -> Reply {
    match r.err.reason {
        Some(reason) => log.emit(cfg, "denied", Code::Ok, json!({"reason": reason})),
        None => log.emit(cfg, "error", Code::Ok, json!({"code": r.err.code})),
    }
    Reply::modern(Some(rpc_error_of(id, &r.err)), r.status)
}

/// Parse one JSON-RPC message, select its era, run the modern checks, take an inflight permit, dispatch, and log.
/// A notification has no body. JSON-RPC protocol errors come back as an error body, never as a transport error
/// (§11); `Err` is a transport denial (throttle, inflight, and their §23 siblings).
pub async fn dispatch_call(
    app: &App,
    cfg: &Config,
    log: &mut CallLog,
    ctx: Ctx<'_>,
    body: &[u8],
) -> Result<Reply, Status> {
    let Ok(rpc) = serde_json::from_slice::<Value>(body) else {
        log.emit(cfg, "error", Code::Ok, json!({"parse": "invalid json"}));
        return Ok(Reply::legacy(Some(rpc_error(
            Value::Null,
            -32700,
            "parse error",
        ))));
    };
    let id = rpc.get("id").cloned().unwrap_or(Value::Null);
    let method = rpc["method"].as_str().unwrap_or("").to_string();
    if method.is_empty() {
        log.emit(cfg, "error", Code::Ok, Value::Null);
        return Ok(Reply::legacy(Some(rpc_error(
            id,
            -32600,
            "invalid request: method missing",
        ))));
    }
    let params = rpc.get("params").cloned().unwrap_or(json!({}));
    log.method = Some(method.clone());
    log.name = mcp::target_name(&method, &params);
    // §23.1: era → modern checks 1–5, each answered with the request id, all before throttle and inflight
    let is_modern = modern::is_modern(ctx.http, &params);
    let no_caps = json!({});
    let mut caps = &no_caps;
    if is_modern {
        log.protocol = Some(modern::MODERN.into());
        log.trace = modern::trace(ctx.http, &params);
        caps = match modern::envelope(ctx.http, &params) {
            Ok(c) => c,
            Err(r) => return Ok(refuse(cfg, log, id, r)),
        };
        if let Some(headers) = ctx.http {
            // `Mcp-Param-*` is only compared for a tool this caller may see: a mismatch on a hidden one would
            // say that it exists
            let tool = match (method.as_str(), params.get("name").and_then(Value::as_str)) {
                ("tools/call", Some(n)) if !cfg.blocked.iter().any(|b| b == n) => {
                    mcp::loaded_tool(&app.sidecar, n).await
                }
                _ => None,
            };
            let policy = mcp::Policy {
                blocked: &cfg.blocked,
                tool_access: &cfg.tool_access,
                kind: ctx.kind,
                roles: &cfg.roles,
            };
            let tool = tool.filter(|_| policy.denial(&method, &params).is_none());
            if let Err(r) = modern::standard_headers(headers, &method, &params, tool.as_ref()) {
                return Ok(refuse(cfg, log, id, r));
            }
        }
        if method.starts_with("notifications/") {
            log.emit(cfg, "ok", Code::Ok, json!({}));
            return Ok(Reply::modern(None, 202));
        }
        if !modern::known_method(&method, ctx.http.is_some()) {
            let err = RpcErr::new(-32601, format!("method not found: {method}"));
            return Ok(refuse(cfg, log, id, modern::Refusal { err, status: 404 }));
        }
    } else {
        // the version a 2025 client named, else the one the node has always answered with
        let named = ctx
            .http
            .and_then(|h| modern::header(h, modern::PROTOCOL_HEADER))
            .and_then(Result::ok);
        log.protocol = Some(named.unwrap_or(modern::LEGACY[0]).to_string());
    }
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
    if is_modern && method == "subscriptions/listen" {
        return listen(app, cfg, log, &ctx, id, &params, caps).await;
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
        kind: ctx.kind,
        roles: &cfg.roles,
    };
    // C10: `tool_hidden` / `tool_denied` — like `blocked_name`, the wire answer comes from `mcp::dispatch`.
    let access_denial = policy.denial(&method, &params);
    // §23.5: a call that would start a task is refused like inflight when the pod already runs its share
    if is_modern
        && method == "tools/call"
        && !blocked
        && access_denial.is_none()
        && modern::tasks_declared(caps)
        && app.tasks.live() >= cfg.max_tasks
        && let Some(name) = params.get("name").and_then(Value::as_str)
        && mcp::is_task(&app.sidecar, name).await
    {
        app.metrics.record(false);
        return Err(log.deny(
            cfg,
            Status::resource_exhausted("busy: max tasks reached"),
            "max_tasks",
        ));
    }
    let env = mcp::Env {
        sc: &app.sidecar,
        modern: is_modern,
        caps,
        progress: progress_fn(&ctx, &method, &params),
        host: Some(mcp::Host {
            app,
            cfg,
            binding: ctx.binding,
        }),
    };
    let out = mcp::dispatch_env(&env, &policy, &method, &params).await;
    // §23.1: the modern envelope and error codes are put on here, so `mcp` raises one set of errors
    let out = match out {
        Ok(Some(r)) if is_modern => Ok(Some(modern::decorate(&method, r, cfg.list_ttl_ms))),
        Err(e) if is_modern => Err(modern::map_error(&method, &params, e)),
        other => other,
    };
    let ok = match &out {
        Ok(Some(r)) => !r.get("isError").and_then(Value::as_bool).unwrap_or(false),
        Ok(None) => true,
        Err(_) => false,
    };
    app.metrics.record(ok);
    app.metrics
        .record_protocol(log.protocol.as_deref().unwrap_or(""));
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
    // §23.5: `tasks/*` lines name the tool and the task; the call that created one names the task too
    if is_modern {
        let task = match (&out, method.as_str()) {
            (Ok(Some(r)), "tools/call") => r.get("taskId").and_then(Value::as_str),
            (Ok(_), "tasks/get" | "tasks/update" | "tasks/cancel") => {
                params.get("taskId").and_then(Value::as_str)
            }
            _ => None,
        };
        if let Some(t) = task {
            extra["task"] = json!(t);
            if method != "tools/call" {
                log.name = app
                    .tasks
                    .get(cfg, ctx.binding, t)
                    .await
                    .ok()
                    .and_then(|r| tasks::tool_of(&r));
            }
        }
    }
    let status = if blocked {
        extra["reason"] = json!("blocked_name");
        "denied"
    } else if let Some(reason) = access_denial {
        extra["reason"] = json!(reason);
        "denied"
    } else if let Some(reason) = out.as_ref().ok().and_then(|o| guardrail_reason(o.as_ref())) {
        // §21.2: the runtime refused the call (or its output) on a rail; `_meta` stays on the result
        extra["reason"] = json!(reason);
        "denied"
    } else if let Some(reason) = out.as_ref().err().and_then(|e| e.reason) {
        // §23.4: `state_invalid`
        extra["reason"] = json!(reason);
        "denied"
    } else if ok {
        "ok"
    } else {
        "error"
    };
    log.emit(cfg, status, Code::Ok, extra);
    let http_status = match &out {
        Err(e) if is_modern => modern::http_status(e.code),
        _ => 200,
    };
    let body = match out {
        Ok(Some(result)) => Some(json!({"jsonrpc": "2.0", "id": id, "result": result})),
        Ok(None) => None,
        Err(e) => Some(rpc_error_of(id, &e)),
    };
    Ok(Reply {
        body,
        status: http_status,
        modern: is_modern,
        listen: None,
    })
}

/// §23.4: the watcher handed to the sidecar for this call — `Some` when the request carries a `progressToken`, the
/// method can report progress, and the transport gave us somewhere to send it. Each runtime line becomes one
/// `notifications/progress`.
fn progress_fn(ctx: &Ctx<'_>, method: &str, params: &Value) -> Option<ProgressFn> {
    let tx = ctx.progress.clone()?;
    if !matches!(method, "tools/call" | "prompts/get") {
        return None;
    }
    let token = progress_token(params)?;
    Some(Arc::new(move |p: &Value| {
        let mut out = json!({"progressToken": token, "progress": p["progress"]});
        for k in ["total", "message"] {
            if let Some(v) = p.get(k).filter(|v| !v.is_null()) {
                out[k] = v.clone();
            }
        }
        let _ =
            tx.send(json!({"jsonrpc": "2.0", "method": "notifications/progress", "params": out}));
    }))
}

/// `params._meta.progressToken`, a string or a number.
pub fn progress_token(params: &Value) -> Option<Value> {
    params
        .pointer("/_meta/progressToken")
        .filter(|t| t.is_string() || t.is_number())
        .cloned()
}

/// §23.6 `subscriptions/listen`: no inflight permit, its own cap. The ack is the stream's first frame.
async fn listen(
    app: &App,
    cfg: &Config,
    log: &mut CallLog,
    ctx: &Ctx<'_>,
    id: Value,
    params: &Value,
    caps: &Value,
) -> Result<Reply, Status> {
    if id.is_null() {
        let err = RpcErr::new(-32600, "invalid request: subscriptions/listen needs an id");
        log.emit(cfg, "error", Code::Ok, json!({"code": err.code}));
        return Ok(Reply::modern(Some(rpc_error_of(id, &err)), 200));
    }
    let asked = params.get("notifications").cloned().unwrap_or(json!({}));
    let with_tasks = modern::tasks_declared(caps);
    let mut watched = Vec::new();
    if let Some(ids) = asked.get("taskIds") {
        // §23.5: asking for task notifications without the extension is `-32021`
        if !with_tasks {
            let err = modern::tasks_required();
            log.emit(cfg, "error", Code::Ok, json!({"code": err.code}));
            return Ok(Reply::modern(Some(rpc_error_of(id, &err)), 400));
        }
        // only the caller's own tasks are accepted; the ack says which
        for t in ids
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            if app.tasks.get(cfg, ctx.binding, t).await.is_ok() {
                watched.push(t.to_string());
            }
        }
    }
    let want = Want::of(&asked, watched);
    let Some(frames) = app.subs.listen(cfg.max_subscriptions, id, want, with_tasks) else {
        app.metrics.record(false);
        return Err(log.deny(
            cfg,
            Status::resource_exhausted("busy: max subscriptions reached"),
            "max_subscriptions",
        ));
    };
    app.metrics.record(true);
    app.metrics.record_protocol(modern::MODERN);
    log.emit(cfg, "ok", Code::Ok, json!({"subscription": "open"}));
    Ok(Reply {
        body: None,
        status: 200,
        modern: true,
        listen: Some(frames),
    })
}

/// §21.2 (0.7.5): `guardrail_pre` / `guardrail_post` when a `tools/call` result carries `_meta.ramen.guardrail`.
pub fn guardrail_reason(result: Option<&Value>) -> Option<&'static str> {
    match result?.pointer("/_meta/ramen/guardrail/stage")?.as_str()? {
        "pre" => Some("guardrail_pre"),
        "post" => Some("guardrail_post"),
        _ => None,
    }
}

pub fn rpc_error(id: Value, code: i64, msg: &str) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": msg}})
}

/// `rpc_error`, with `error.data` when the error carries some (§23.1; never on a 2025-06-18 answer).
pub fn rpc_error_of(id: Value, e: &RpcErr) -> Value {
    let mut v = rpc_error(id, e.code, &e.message);
    if let Some(d) = &e.data {
        v["error"]["data"] = d.clone();
    }
    v
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
        // §23.1: no headers handed over — on gRPC the body alone selects the era, and there is no stream to carry
        // progress or a subscription
        let ctx = Ctx {
            kind: &caller.kind,
            binding: &caller.binding,
            http: None,
            progress: None,
        };
        Ok(
            match dispatch_call(app, &cfg, &mut log, ctx, &body).await?.body {
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

    /// C2: `Admin/Metrics` reports the manifest hash of the last load, `""` before it; §21.2: and the runtime's
    /// `guardrails` block, `{"engine": "none", "tools": {}}` before it or when the runtime reports none.
    #[tokio::test]
    async fn metrics_report_the_manifest_hash() {
        let a = app(cfg(&[])).await;
        assert_eq!(a.metrics_json().await["manifest_hash"], "");
        assert_eq!(
            a.metrics_json().await["guardrails"],
            json!({"engine": "none", "tools": {}})
        );
        a.sidecar
            .set_loaded_for_test(json!({"tools": [], "hash": "deadbeef"}))
            .await;
        assert_eq!(a.metrics_json().await["manifest_hash"], "deadbeef");
        assert_eq!(a.metrics_json().await["guardrails"]["engine"], "none");
        a.sidecar
            .set_loaded_for_test(json!({"tools": [], "hash": "deadbeef",
                "guardrails": {"engine": "nemo", "fail": "closed", "tools": {"calc": ["pre"]}}}))
            .await;
        assert_eq!(a.metrics_json().await["guardrails"]["engine"], "nemo");
        assert_eq!(
            a.metrics_json().await["guardrails"]["tools"]["calc"][0],
            "pre"
        );
    }

    /// §23.1: the body selects the era on `dispatch_body` too; "not loaded" is `-32603` there and stays `-32002`
    /// on the legacy era; `server/discover` answers before any load and without the tasks extension.
    #[tokio::test]
    async fn the_body_selects_the_era_and_not_loaded_is_internal_on_the_modern_one() {
        let c = cfg(&[]);
        let a = app(c.clone()).await;
        let call = |body: Value| {
            let (a, c) = (a.clone(), c.clone());
            async move {
                let mut log = CallLog::new("127.0.0.1".parse().unwrap(), "grpc");
                dispatch_body(&a, &c, &mut log, "key", body.to_string().as_bytes())
                    .await
                    .unwrap()
                    .unwrap()
            }
        };
        let meta = json!({modern::META_VERSION: modern::MODERN, modern::META_CAPS: {}});
        let m = |method: &str| json!({"jsonrpc": "2.0", "id": 5, "method": method, "params": {"_meta": meta}});
        let out = call(m("tools/list")).await;
        assert_eq!(
            (out["error"]["code"].as_i64(), out["id"].as_i64()),
            (Some(-32603), Some(5)),
            "{out}"
        );
        let out = call(json!({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})).await;
        assert_eq!(out["error"]["code"], -32002);
        let out = call(m("server/discover")).await;
        assert_eq!(out["result"]["resultType"], "complete", "{out}");
        assert!(out["result"]["capabilities"].get("extensions").is_none());
        assert_eq!(out["result"]["ttlMs"], 60_000);
        // completion before any load: nothing to suggest
        let mut complete = m("completion/complete");
        complete["params"]["ref"] = json!({"type": "ref/prompt", "name": "p"});
        complete["params"]["argument"] = json!({"name": "a", "value": ""});
        assert_eq!(
            call(complete).await["result"]["completion"]["values"],
            json!([])
        );
        // a task method without a store behind it cannot happen through here: the host is always set
        let mut get = m("tasks/get");
        get["params"]["taskId"] = json!("x");
        get["params"]["_meta"][modern::META_CAPS] = json!({"extensions": {modern::TASKS_EXT: {}}});
        assert_eq!(
            call(get).await["error"],
            json!({"code": -32602, "message": "Unknown task"})
        );
        // a `subscriptions/listen` without an id is not a subscription
        let mut log = CallLog::new("127.0.0.1".parse().unwrap(), "http");
        let headers = HeaderMap::new();
        let ctx = Ctx {
            kind: "key",
            binding: "b",
            http: Some(&headers),
            progress: None,
        };
        let body =
            json!({"jsonrpc": "2.0", "method": "subscriptions/listen", "params": {"_meta": meta}});
        let reply = dispatch_call(&a, &c, &mut log, ctx, body.to_string().as_bytes())
            .await
            .unwrap();
        // ...and here it never gets that far: the standard headers are missing, which is refused first
        assert_eq!(
            (reply.status, reply.body.unwrap()["error"]["code"].as_i64()),
            (400, Some(-32020))
        );
        assert_eq!(a.subs.open(), 0);
    }

    /// §23.4: a progress watcher exists only for a call that named a token, on a method that can report.
    #[test]
    fn progress_needs_a_token_a_sink_and_a_reporting_method() {
        let (tx, mut rx) = mpsc::unbounded_channel();
        let ctx = |p: Option<mpsc::UnboundedSender<Value>>| Ctx {
            kind: "key",
            binding: "b",
            http: None,
            progress: p,
        };
        let with = json!({"_meta": {"progressToken": "t1"}});
        assert!(progress_fn(&ctx(None), "tools/call", &with).is_none());
        assert!(progress_fn(&ctx(Some(tx.clone())), "tools/list", &with).is_none());
        assert!(progress_fn(&ctx(Some(tx.clone())), "tools/call", &json!({})).is_none());
        let bad = json!({"_meta": {"progressToken": {"no": 1}}});
        assert!(progress_fn(&ctx(Some(tx.clone())), "tools/call", &bad).is_none());
        let f = progress_fn(&ctx(Some(tx)), "prompts/get", &with).expect("a watcher");
        f(&json!({"id": 9, "progress": 3, "total": null, "message": "m"}));
        assert_eq!(
            rx.try_recv().unwrap(),
            json!({"jsonrpc": "2.0", "method": "notifications/progress",
                   "params": {"progressToken": "t1", "progress": 3, "message": "m"}})
        );
    }

    /// §21.2 (0.7.5): the access-log reason for a call the runtime refused on a rail.
    #[test]
    fn guardrail_reason_reads_the_result_meta() {
        assert_eq!(
            guardrail_reason(Some(
                &json!({"isError": true, "_meta": {"ramen": {"guardrail": {"stage": "pre", "engine": "nemo"}}}})
            )),
            Some("guardrail_pre")
        );
        assert_eq!(
            guardrail_reason(Some(
                &json!({"isError": true, "_meta": {"ramen": {"guardrail": {"stage": "post"}}}})
            )),
            Some("guardrail_post")
        );
        assert_eq!(guardrail_reason(Some(&json!({"isError": true}))), None);
        assert_eq!(guardrail_reason(None), None);
    }
}
