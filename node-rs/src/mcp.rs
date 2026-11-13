//! MCP JSON-RPC methods (CONTRACTS §3 methods, carried over gRPC per §11) → sidecar `runtime.*` calls.
use crate::config::{Config, ToolAccess};
use crate::grpc::App;
use crate::modern;
use crate::sidecar::{ProgressFn, RpcErr, Sidecar};
use crate::tasks;
use serde_json::{Value, json};
use std::collections::HashMap;

pub const PROTOCOL_VERSION: &str = "2025-06-18";
/// The caller kind that is allowed everything and never listed in `RAMEN_TOOL_ACCESS` (C10).
pub const SUPER_ADMIN: &str = "super_admin";

/// What one caller may see and call: `blocked` (§9, `RAMEN_BLOCKED`) hides names from everyone; `tool_access`
/// (C10, `RAMEN_TOOL_ACCESS`) narrows tools per caller `kind` (`key`, or a token's `role`).
#[derive(Clone, Copy)]
pub struct Policy<'a> {
    pub blocked: &'a [String],
    pub tool_access: &'a HashMap<String, ToolAccess>,
    pub kind: &'a str,
    /// §21.4: custom role → base role (`RAMEN_ROLES`); a list naming either admits the caller.
    pub roles: &'a HashMap<String, String>,
}

impl Policy<'_> {
    fn allows(&self, name: &str, which: fn(&ToolAccess) -> &Vec<String>) -> bool {
        self.kind == SUPER_ADMIN
            || self.tool_access.get(name).is_none_or(|a| {
                let base = self.roles.get(self.kind);
                which(a)
                    .iter()
                    .any(|k| k == self.kind || base.is_some_and(|b| b == k))
            })
    }
    /// Whether `tools/list` shows this tool to the caller (the blocked list is applied separately).
    pub fn tool_listable(&self, name: &str) -> bool {
        self.allows(name, |a| &a.list)
    }
    pub fn tool_callable(&self, name: &str) -> bool {
        self.allows(name, |a| &a.call)
    }
    /// The access-log reason a `tools/call` is refused by this policy: `tool_hidden` (answered `-32601`) or
    /// `tool_denied` (answered `-32003`). `None` when the call is allowed or the method is not `tools/call`.
    pub fn denial(&self, method: &str, params: &Value) -> Option<&'static str> {
        if method != "tools/call" {
            return None;
        }
        let name = params.get("name").and_then(Value::as_str)?;
        if !self.tool_listable(name) {
            Some("tool_hidden")
        } else if !self.tool_callable(name) {
            Some("tool_denied")
        } else {
            None
        }
    }
}

/// What `dispatch_env` needs from the node beyond the sidecar, on a real call: the app (signing key, task store,
/// subscription hub), the live config, and the caller binding of §16.2.
#[derive(Clone, Copy)]
pub struct Host<'a> {
    pub app: &'a App,
    pub cfg: &'a Config,
    pub binding: &'a str,
}

/// One call's era and context (§23). `Env::legacy` is the 2025-06-18 world with nothing attached.
pub struct Env<'a> {
    pub sc: &'a Sidecar,
    /// §23.1: served as 2026-07-28.
    pub modern: bool,
    /// The client capabilities declared on this request (`{}` on the legacy era).
    pub caps: &'a Value,
    /// §23.4: where this call's `runtime.progress` lines go, when the client asked and can take a stream.
    pub progress: Option<ProgressFn>,
    pub host: Option<Host<'a>>,
    /// §24.3: the call's span; each sidecar round trip becomes a child of it.
    pub span: Option<&'a crate::otel::SpanCtx>,
}

static NO_CAPS: Value = Value::Null;

impl<'a> Env<'a> {
    pub fn legacy(sc: &'a Sidecar) -> Self {
        Env {
            sc,
            modern: false,
            caps: &NO_CAPS,
            progress: None,
            host: None,
            span: None,
        }
    }
}

/// One sidecar round trip for this call, timed as the child span `runtime <call>` when the call has a span (§24.3).
async fn runtime(env: &Env<'_>, method: &str, params: Value) -> Result<Value, RpcErr> {
    let start = std::time::SystemTime::now();
    let r = env
        .sc
        .call_watched(method, params, env.progress.clone())
        .await;
    if let Some(span) = env.span {
        let call = method.strip_prefix("runtime.").unwrap_or(method);
        span.child(format!("runtime {call}"), start, r.is_ok());
    }
    r
}

/// The 2025-06-18 dispatch, as every caller before 1.0.0 knew it.
pub async fn dispatch(
    sc: &Sidecar,
    policy: &Policy<'_>,
    method: &str,
    params: &Value,
) -> Result<Option<Value>, RpcErr> {
    dispatch_env(&Env::legacy(sc), policy, method, params).await
}

/// `Ok(None)` for notifications (no response body). Blocked names / resource URIs are hidden from the list calls
/// and answered `-32601` when called; a tool the caller's kind may not list is answered the same way, and one it
/// may list but not call is `-32003 forbidden: <tool> is not callable for <kind>`. Errors are raised with their
/// 2025-06-18 codes on either era; `grpc::dispatch_call` passes a modern one through `modern::map_error` (§23.1).
pub async fn dispatch_env(
    env: &Env<'_>,
    policy: &Policy<'_>,
    method: &str,
    params: &Value,
) -> Result<Option<Value>, RpcErr> {
    if method.starts_with("notifications/") {
        return Ok(None);
    }
    let sc = env.sc;
    let blocked = policy.blocked;
    let r = match method {
        "initialize" if !env.modern => {
            // C11: list changes are announced on the first SSE-capable response after a rollout (http.rs)
            json!({"protocolVersion": PROTOCOL_VERSION,
                   "capabilities": {"tools": {"listChanged": true}, "resources": {}, "prompts": {}},
                   "serverInfo": {"name": "ramen-node", "version": env!("CARGO_PKG_VERSION")}})
        }
        "ping" if !env.modern => json!({}),
        "server/discover" if env.modern => {
            // §23.1: `extensions` only when a loaded tool can run as a task; no `logging`, no `resources.subscribe`
            let mut caps = json!({"tools": {"listChanged": true}, "prompts": {"listChanged": true},
                                  "resources": {"listChanged": true}, "completions": {}});
            let tasks = sc.loaded().await.is_some_and(|l| {
                l.result["tools"]
                    .as_array()
                    .is_some_and(|ts| ts.iter().any(is_task_tool))
            });
            if tasks {
                caps["extensions"] = json!({modern::TASKS_EXT: {}});
            }
            json!({"supportedVersions": modern::supported(), "capabilities": caps})
        }
        "tools/list" => {
            let tools = loaded(sc, "tools", blocked).await?;
            let visible = match tools {
                Value::Array(items) if !policy.tool_access.is_empty() => Value::Array(
                    items
                        .into_iter()
                        .filter(|t| {
                            t.get("name")
                                .and_then(Value::as_str)
                                .is_none_or(|n| policy.tool_listable(n))
                        })
                        .collect(),
                ),
                other => other,
            };
            json!({"tools": visible})
        }
        "resources/list" => json!({"resources": loaded(sc, "resources", blocked).await?}),
        // §23.3: templates are listed here and not by `resources/list`; a 0.8 runtime reports none
        "resources/templates/list" => match loaded(sc, "resourceTemplates", blocked).await? {
            Value::Null => json!({"resourceTemplates": []}),
            templates => json!({"resourceTemplates": templates}),
        },
        "prompts/list" => json!({"prompts": loaded(sc, "prompts", blocked).await?}),
        "tools/call" => {
            let name = str_param(params, "name")?;
            deny_if_blocked(blocked, name, "tool")?;
            if !policy.tool_listable(name) {
                return Err(RpcErr::new(-32601, format!("tool not found: {name}")));
            }
            if !policy.tool_callable(name) {
                return Err(RpcErr::new(
                    -32003,
                    format!("forbidden: {name} is not callable for {}", policy.kind),
                ));
            }
            let arguments = params.get("arguments").cloned().unwrap_or(json!({}));
            // N8: the sidecar validates the same schema (executor.py `jsonschema.validate`); catching it here
            // too saves the round trip and matches the exact result shape a sidecar validation failure uses
            // (`isError: true`, not a JSON-RPC error — argument problems are a tool-call outcome, not a
            // protocol fault, CONTRACTS §2).
            if let Some(msg) = invalid_tool_args(sc, name, &arguments).await {
                return Ok(Some(tool_error(msg)));
            }
            let mut call = json!({"name": name, "arguments": arguments});
            round_params(env, method, name, params, &mut call)?;
            // §23.5: a task tool runs as a task only for a client that declared the extension on this request
            if let Some(host) = env.host.filter(|_| env.modern)
                && modern::tasks_declared(env.caps)
                && is_task(sc, name).await
            {
                let app = host.app;
                let start = std::time::SystemTime::now();
                let started = app
                    .tasks
                    .start(
                        &app.sidecar,
                        &app.subs,
                        host.cfg,
                        host.binding,
                        name,
                        &call["arguments"],
                        env.caps,
                    )
                    .await;
                if let Some(span) = env.span {
                    span.child("runtime task_start".into(), start, started.is_ok());
                }
                let mut task = started?;
                task["resultType"] = json!("task");
                return Ok(Some(task));
            }
            let r = runtime(env, "runtime.call_tool", call).await?;
            match round_result(env, method, name, params, r)? {
                Round::Done(r) => r,
                Round::Legacy(msg) => tool_error(msg),
            }
        }
        "resources/read" => {
            let uri = str_param(params, "uri")?;
            deny_if_blocked(blocked, uri, "resource")?;
            if let Some(name) = resource_name(sc, uri).await {
                deny_if_blocked(blocked, &name, "resource")?;
            }
            runtime(env, "runtime.read_resource", json!({"uri": uri})).await?
        }
        "prompts/get" => {
            let name = str_param(params, "name")?;
            deny_if_blocked(blocked, name, "prompt")?;
            let arguments = params.get("arguments").cloned().unwrap_or(json!({}));
            // N8: mirrors executor.py `get_prompt`'s missing-argument `ValueError`, which the sidecar maps to
            // INVALID_PARAMS (-32602) — a real JSON-RPC error here, unlike tools/call (prompts/get has no
            // isError-shaped result in the MCP spec).
            if let Some(msg) = missing_prompt_args(sc, name, &arguments).await {
                return Err(RpcErr::new(-32602, msg));
            }
            let mut call = json!({"name": name, "arguments": arguments});
            round_params(env, method, name, params, &mut call)?;
            let r = runtime(env, "runtime.get_prompt", call).await?;
            match round_result(env, method, name, params, r)? {
                Round::Done(r) => r,
                Round::Legacy(msg) => return Err(RpcErr::new(-32602, msg)),
            }
        }
        "completion/complete" => complete(sc, blocked, params).await?,
        "tasks/get" | "tasks/update" | "tasks/cancel" if env.modern => {
            // §23.5: without the extension declared on this request the methods do not exist for the caller
            if !modern::tasks_declared(env.caps) {
                return Err(modern::tasks_required());
            }
            let host = env.host.ok_or_else(|| RpcErr::internal("no task store"))?;
            let id = str_param(params, "taskId")?;
            let (tasks, cfg, who) = (&host.app.tasks, host.cfg, host.binding);
            match method {
                "tasks/get" => tasks::public(&tasks.get(cfg, who, id).await?),
                "tasks/update" => {
                    let responses = input_responses(params)?.unwrap_or_else(|| json!({}));
                    tasks.update(cfg, who, id, &responses).await?;
                    json!({})
                }
                _ => {
                    tasks.cancel(cfg, who, id).await?;
                    json!({})
                }
            }
        }
        _ => return Err(RpcErr::new(-32601, format!("method not found: {method}"))),
    };
    Ok(Some(r))
}

fn tool_error(msg: String) -> Value {
    json!({"content": [{"type": "text", "text": msg}], "isError": true})
}

/// §23.5: a loaded tool the runtime marked as able to run as a task (`"task": true` in its proto).
pub fn is_task_tool(tool: &Value) -> bool {
    tool.get("task").and_then(Value::as_bool) == Some(true)
        || matches!(
            tool.pointer("/execution/taskSupport")
                .and_then(Value::as_str),
            Some("optional" | "required")
        )
        || tool
            .get("_meta")
            .and_then(|m| m.get(modern::TASKS_EXT))
            .is_some()
}

/// The loaded tool of this name, as `tools/list` would show it.
pub async fn loaded_tool(sc: &Sidecar, name: &str) -> Option<Value> {
    let l = sc.loaded().await?;
    l.result["tools"]
        .as_array()?
        .iter()
        .find(|t| t.get("name").and_then(Value::as_str) == Some(name))
        .cloned()
}

pub async fn is_task(sc: &Sidecar, name: &str) -> bool {
    loaded_tool(sc, name)
        .await
        .is_some_and(|t| is_task_tool(&t))
}

/// §23.4: `inputResponses` is an object of objects when present.
fn input_responses(params: &Value) -> Result<Option<Value>, RpcErr> {
    match params.get("inputResponses") {
        None => Ok(None),
        Some(Value::Object(o)) if o.values().all(Value::is_object) => {
            Ok(Some(Value::Object(o.clone())))
        }
        Some(_) => Err(RpcErr::new(
            -32602,
            "inputResponses must be an object of objects",
        )),
    }
}

fn state_binding<'a>(
    host: &Host<'a>,
    method: &'a str,
    name: &'a str,
    args: &'a str,
) -> modern::StateBinding<'a> {
    modern::StateBinding {
        caller: host.binding,
        group: &host.cfg.group,
        zone: &host.cfg.zone,
        method,
        name,
        args,
    }
}

/// §23.4: what a `runtime.call_tool` / `runtime.get_prompt` gains — the declared capabilities, whether progress
/// lines are wanted, and on a modern retry the client's `inputResponses` and the `state` the node verified. A
/// 2025-06-18 request has no retry: its `inputResponses` / `requestState` are not looked at.
fn round_params(
    env: &Env<'_>,
    method: &str,
    name: &str,
    params: &Value,
    call: &mut Value,
) -> Result<(), RpcErr> {
    let caps = if env.caps.is_object() {
        env.caps.clone()
    } else {
        json!({})
    };
    call["client"] = json!({"capabilities": caps});
    call["progress"] = json!(env.progress.is_some());
    let Some(host) = env.host.filter(|_| env.modern) else {
        return Ok(());
    };
    if let Some(r) = input_responses(params)? {
        call["inputResponses"] = r;
    }
    if let Some(token) = params.get("requestState") {
        let refused = || {
            RpcErr::new(-32602, "requestState integrity check failed")
                .because(modern::STATE_INVALID)
        };
        let args = crate::grpc::args_digest(params.get("arguments"));
        let state = modern::verify_state(
            &host.app.mrtr_key,
            &state_binding(&host, method, name, &args),
            token.as_str().ok_or_else(refused)?,
            crate::session::now(),
        )
        .map_err(|_| refused())?;
        call["state"] = state;
    }
    Ok(())
}

enum Round {
    Done(Value),
    /// The 2025-06-18 downgrade text: a tool error for `tools/call`, `-32602` for `prompts/get`.
    Legacy(String),
}

/// §23.4: the runtime's answer to one round. `input_required` goes to a modern client that declared what is asked
/// (with the tool's `state` signed into `requestState`); an undeclared ask and `missing_capability` are `-32021`.
fn round_result(
    env: &Env<'_>,
    method: &str,
    name: &str,
    params: &Value,
    r: Value,
) -> Result<Round, RpcErr> {
    let keys = |v: Option<&Value>| -> String {
        v.and_then(Value::as_object)
            .map(|o| o.keys().cloned().collect::<Vec<_>>().join(", "))
            .unwrap_or_default()
    };
    match r.get("resultType").and_then(Value::as_str) {
        Some("input_required") => {
            let host = match env.host.filter(|_| env.modern) {
                Some(h) => h,
                None => {
                    return Ok(Round::Legacy(format!(
                        "this tool needs more input ({}); it can only ask a client that speaks MCP {}",
                        keys(r.get("inputRequests")),
                        modern::MODERN
                    )));
                }
            };
            let requests = r.get("inputRequests").cloned().unwrap_or_else(|| json!({}));
            if let Some(required) = modern::undeclared(&requests, env.caps)? {
                return Err(modern::missing_capability(required));
            }
            let mut out = json!({"resultType": "input_required", "inputRequests": requests});
            if let Some(state) = r.get("state").filter(|s| !s.is_null()) {
                let args = crate::grpc::args_digest(params.get("arguments"));
                out["requestState"] = json!(modern::sign_state(
                    &host.app.mrtr_key,
                    &state_binding(&host, method, name, &args),
                    state,
                    crate::session::now() + host.cfg.mrtr_ttl_secs,
                ));
            }
            Ok(Round::Done(out))
        }
        Some("missing_capability") => {
            let required = r
                .get("requiredCapabilities")
                .cloned()
                .unwrap_or_else(|| json!({}));
            if env.modern {
                return Err(modern::missing_capability(required));
            }
            Ok(Round::Legacy(format!(
                "this tool needs a client capability ({}); it can only run for a client that speaks MCP {}",
                keys(Some(&required)),
                modern::MODERN
            )))
        }
        _ => Ok(Round::Done(r)),
    }
}

/// §23.1 `completion/complete` (both eras): the `enum` entries of the named prompt argument or template variable
/// that start with what was typed, at most 100; nothing declared, nothing suggested.
async fn complete(sc: &Sidecar, blocked: &[String], params: &Value) -> Result<Value, RpcErr> {
    let bad = || RpcErr::new(-32602, "ref and argument.name are required");
    let kind = params
        .pointer("/ref/type")
        .and_then(Value::as_str)
        .ok_or_else(bad)?;
    let arg = params
        .pointer("/argument/name")
        .and_then(Value::as_str)
        .ok_or_else(bad)?;
    let typed = params
        .pointer("/argument/value")
        .and_then(Value::as_str)
        .unwrap_or("");
    let (list, key, wanted) = match kind {
        "ref/prompt" => ("prompts", "name", params.pointer("/ref/name")),
        "ref/resource" => (
            "resourceTemplates",
            "uriTemplate",
            params.pointer("/ref/uri"),
        ),
        _ => return Err(bad()),
    };
    let wanted = wanted.and_then(Value::as_str).ok_or_else(bad)?;
    let item = sc.loaded().await.and_then(|l| {
        l.result[list]
            .as_array()?
            .iter()
            .find(|i| i.get(key).and_then(Value::as_str) == Some(wanted))
            .filter(|i| !is_blocked(blocked, i))
            .cloned()
    });
    let values: Vec<Value> = item
        .as_ref()
        .and_then(|i| declared_enum(i, arg))
        .into_iter()
        .flatten()
        .filter(|v| v.as_str().is_some_and(|s| s.starts_with(typed)))
        .take(100)
        .cloned()
        .collect();
    Ok(json!({"completion": {"total": values.len(), "values": values, "hasMore": false}}))
}

/// The `enum` a prompt or resource template declares for one argument / variable: an `arguments` entry of that
/// name, or (templates, §23.3) `variables.<var>.enum` as the runtime's load result spells it.
fn declared_enum<'a>(item: &'a Value, arg: &str) -> Option<&'a Vec<Value>> {
    item.get("arguments")
        .and_then(Value::as_array)
        .and_then(|a| {
            a.iter()
                .find(|e| e.get("name").and_then(Value::as_str) == Some(arg))
        })
        .and_then(|e| e.get("enum"))
        .or_else(|| item.pointer(&format!("/variables/{arg}/enum")))
        .and_then(Value::as_array)
}

/// Whether this call targets a blocked name or URI (§9) — the access log's `blocked_name` reason (C6).
/// `dispatch` still answers the `-32601` itself; this only classifies the call for the log.
pub async fn blocked_target(
    sc: &Sidecar,
    blocked: &[String],
    method: &str,
    params: &Value,
) -> bool {
    if blocked.is_empty() {
        return false;
    }
    let hit = |n: &str| blocked.iter().any(|b| b == n);
    match method {
        "tools/call" | "prompts/get" => params.get("name").and_then(Value::as_str).is_some_and(hit),
        "resources/read" => match params.get("uri").and_then(Value::as_str) {
            Some(uri) => hit(uri) || resource_name(sc, uri).await.is_some_and(|n| hit(&n)),
            None => false,
        },
        _ => false,
    }
}

pub fn is_blocked(blocked: &[String], item: &Value) -> bool {
    ["name", "uri", "uriTemplate"]
        .iter()
        .filter_map(|k| item.get(k).and_then(Value::as_str))
        .any(|v| blocked.iter().any(|b| b == v))
}

fn deny_if_blocked(blocked: &[String], name: &str, kind: &str) -> Result<(), RpcErr> {
    if blocked.iter().any(|b| b == name) {
        return Err(RpcErr::new(-32601, format!("{kind} not found: {name}")));
    }
    Ok(())
}

/// N8: `None` when the tool is unknown (unchanged behaviour: let the sidecar report that) or the arguments
/// are valid; `Some(message)` matches the text `executor.py`'s `jsonschema.ValidationError` handler puts in
/// an `isError: true` result.
async fn invalid_tool_args(sc: &Sidecar, name: &str, arguments: &Value) -> Option<String> {
    let l = sc.loaded().await?;
    let schema = l.result["tools"]
        .as_array()?
        .iter()
        .find(|t| t.get("name").and_then(Value::as_str) == Some(name))?
        .get("inputSchema")?;
    validate_schema(schema, arguments)
}

/// Every `inputSchema` here is flat and scalar-only by construction (runtime-py `proto.input_schema`): one
/// level of properties, each a plain JSON type or a string `enum`, every property required, no extras.
fn validate_schema(schema: &Value, arguments: &Value) -> Option<String> {
    let Some(obj) = arguments.as_object() else {
        return Some("arguments must be an object".into());
    };
    if let Some(required) = schema.get("required").and_then(Value::as_array) {
        for r in required.iter().filter_map(Value::as_str) {
            if !obj.contains_key(r) {
                return Some(format!("missing required argument: {r}"));
            }
        }
    }
    let empty = serde_json::Map::new();
    let props = schema
        .get("properties")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    for (k, v) in obj {
        let Some(prop) = props.get(k) else {
            return Some(format!("unexpected argument: {k}"));
        };
        if let Some(allowed) = prop.get("enum").and_then(Value::as_array) {
            let ok = v
                .as_str()
                .is_some_and(|s| allowed.iter().any(|a| a.as_str() == Some(s)));
            if !ok {
                return Some(format!("argument {k} must be one of {allowed:?}"));
            }
            continue;
        }
        let want = prop.get("type").and_then(Value::as_str).unwrap_or("string");
        let ok = match want {
            "string" => v.is_string(),
            "number" => v.is_number(),
            "integer" => v.is_i64() || v.is_u64(),
            "boolean" => v.is_boolean(),
            _ => true,
        };
        if !ok {
            return Some(format!("argument {k} must be type {want}"));
        }
    }
    None
}

/// N8: mirrors `executor.py`'s `get_prompt` missing-argument check. Prompts carry no type info in their
/// MCP-facing `arguments` list (only `name`/`required`), so presence is all that is checkable here.
async fn missing_prompt_args(sc: &Sidecar, name: &str, arguments: &Value) -> Option<String> {
    let l = sc.loaded().await?;
    let prompt = l.result["prompts"]
        .as_array()?
        .iter()
        .find(|p| p.get("name").and_then(Value::as_str) == Some(name))?;
    let have = arguments.as_object();
    let missing: Vec<&str> = prompt
        .get("arguments")
        .and_then(Value::as_array)?
        .iter()
        .filter_map(|a| a.get("name").and_then(Value::as_str))
        .filter(|n| !have.is_some_and(|o| o.contains_key(*n)))
        .collect();
    (!missing.is_empty()).then(|| format!("missing prompt arguments: {missing:?}"))
}

/// `name` of the loaded resource with this URI (so a resource blocked by name is also unreadable by URI), or of
/// the template the URI matches (§23.3) — a blocked template must not be readable through any of its URIs.
async fn resource_name(sc: &Sidecar, uri: &str) -> Option<String> {
    let l = sc.loaded().await?;
    let named = |r: &Value| r.get("name").and_then(Value::as_str).map(String::from);
    let exact = l.result["resources"]
        .as_array()
        .and_then(|rs| {
            rs.iter()
                .find(|r| r.get("uri").and_then(Value::as_str) == Some(uri))
        })
        .and_then(named);
    exact.or_else(|| {
        l.result["resourceTemplates"]
            .as_array()?
            .iter()
            .find(|t| {
                t.get("uriTemplate")
                    .and_then(Value::as_str)
                    .is_some_and(|tpl| template_matches(tpl, uri))
            })
            .and_then(named)
    })
}

/// Whether `uri` is an instance of `template`, each `{var}` standing for one non-empty path segment (§23.3).
pub fn template_matches(template: &str, uri: &str) -> bool {
    let Some(open) = template.find('{') else {
        return template == uri;
    };
    let Some(rest) = uri.strip_prefix(&template[..open]) else {
        return false;
    };
    let Some(close) = template[open..].find('}') else {
        return false; // an unclosed brace matches nothing
    };
    let after = &template[open + close + 1..];
    let segment = rest.find('/').unwrap_or(rest.len());
    // the variable takes 1..=segment characters; what follows must match the rest of the template
    (1..=segment)
        .rev()
        .filter(|n| rest.is_char_boundary(*n))
        .any(|n| template_matches(after, &rest[n..]))
}

async fn loaded(sc: &Sidecar, key: &str, blocked: &[String]) -> Result<Value, RpcErr> {
    let l = sc
        .loaded()
        .await
        .ok_or_else(|| RpcErr::new(-32002, "runtime not loaded"))?;
    Ok(match l.result[key].as_array() {
        Some(items) if !blocked.is_empty() => Value::Array(
            items
                .iter()
                .filter(|i| !is_blocked(blocked, i))
                .cloned()
                .collect(),
        ),
        _ => l.result[key].clone(),
    })
}

fn str_param<'a>(params: &'a Value, key: &str) -> Result<&'a str, RpcErr> {
    params
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| RpcErr::new(-32602, format!("{key} must be a string")))
}

/// Name of the thing being called, for the access log.
pub fn target_name(method: &str, params: &Value) -> Option<String> {
    match method {
        "tools/call" | "prompts/get" => {
            params.get("name").and_then(Value::as_str).map(String::from)
        }
        "resources/read" => params.get("uri").and_then(Value::as_str).map(String::from),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// No custom roles (the 0.7.2 world): every test that is not about §21.4 runs with this.
    fn no_roles() -> &'static HashMap<String, String> {
        static NONE: std::sync::OnceLock<HashMap<String, String>> = std::sync::OnceLock::new();
        NONE.get_or_init(HashMap::new)
    }

    #[test]
    fn blocked_matches_name_or_uri() {
        let b = vec!["calc".to_string(), "ramen://demo/readme".to_string()];
        assert!(is_blocked(&b, &json!({"name": "calc"})));
        assert!(is_blocked(
            &b,
            &json!({"name": "readme", "uri": "ramen://demo/readme"})
        ));
        assert!(!is_blocked(
            &b,
            &json!({"name": "other", "uri": "ramen://x"})
        ));
        assert!(!is_blocked(&b, &json!({})));
        assert!(deny_if_blocked(&b, "calc", "tool").is_err());
        assert_eq!(
            deny_if_blocked(&b, "calc", "tool").unwrap_err().code,
            -32601
        );
        assert!(deny_if_blocked(&b, "ok", "tool").is_ok());
    }

    fn scalar_schema() -> Value {
        json!({
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "string", "enum": ["x", "y"]}},
            "required": ["a", "b"],
            "additionalProperties": false,
        })
    }

    #[test]
    fn validate_schema_accepts_matching_scalar_types() {
        assert!(validate_schema(&scalar_schema(), &json!({"a": 1, "b": "x"})).is_none());
    }

    #[test]
    fn validate_schema_rejects_wrong_type() {
        let msg = validate_schema(&scalar_schema(), &json!({"a": "nope", "b": "x"})).unwrap();
        assert!(msg.contains('a'), "{msg}");
    }

    #[test]
    fn validate_schema_rejects_an_enum_value_outside_the_list() {
        let msg = validate_schema(&scalar_schema(), &json!({"a": 1, "b": "z"})).unwrap();
        assert!(msg.contains('b'), "{msg}");
    }

    #[test]
    fn validate_schema_rejects_missing_required() {
        let msg = validate_schema(&scalar_schema(), &json!({"a": 1})).unwrap();
        assert!(msg.contains('b'), "{msg}");
    }

    #[test]
    fn validate_schema_rejects_unexpected_extra_argument() {
        let msg = validate_schema(&scalar_schema(), &json!({"a": 1, "b": "x", "c": true})).unwrap();
        assert!(msg.contains('c'), "{msg}");
    }

    #[test]
    fn validate_schema_rejects_a_non_object() {
        assert!(validate_schema(&scalar_schema(), &json!([1, 2])).is_some());
    }

    fn sidecar() -> std::sync::Arc<crate::sidecar::Sidecar> {
        crate::sidecar::Sidecar::new(crate::config::Config::from_map(&Default::default()).unwrap())
    }

    fn tool(name: &str, schema: Value) -> Value {
        json!({"name": name, "description": "", "inputSchema": schema})
    }

    #[tokio::test]
    async fn blocked_target_classifies_calls_by_name_and_by_resource_uri() {
        let sc = sidecar();
        sc.set_loaded_for_test(
            json!({"resources": [{"name": "readme", "uri": "ramen://demo/readme"}]}),
        )
        .await;
        let b = vec!["calc".to_string(), "readme".to_string()];
        assert!(blocked_target(&sc, &b, "tools/call", &json!({"name": "calc"})).await);
        assert!(blocked_target(&sc, &b, "prompts/get", &json!({"name": "calc"})).await);
        assert!(!blocked_target(&sc, &b, "tools/call", &json!({"name": "other"})).await);
        assert!(!blocked_target(&sc, &b, "tools/list", &json!({})).await);
        // a resource blocked by name is also blocked when read by its URI
        assert!(
            blocked_target(
                &sc,
                &b,
                "resources/read",
                &json!({"uri": "ramen://demo/readme"})
            )
            .await
        );
        assert!(!blocked_target(&sc, &b, "resources/read", &json!({"uri": "ramen://other"})).await);
        assert!(!blocked_target(&sc, &[], "tools/call", &json!({"name": "calc"})).await);
    }

    fn access(json: &str) -> HashMap<String, ToolAccess> {
        crate::config::parse_tool_access(json).unwrap()
    }

    /// C10: a tool absent from the map is unrestricted; `super_admin` is never restricted.
    #[test]
    fn policy_decides_list_and_call_per_kind() {
        let m = access(
            r#"{"calc":{"list":["key","viewer"],"call":["key"]},"hidden":{"list":[],"call":[]}}"#,
        );
        let p = |kind: &'static str| Policy {
            blocked: &[],
            tool_access: &m,
            roles: no_roles(),
            kind,
        };
        assert!(p("key").tool_listable("calc") && p("key").tool_callable("calc"));
        assert!(p("viewer").tool_listable("calc") && !p("viewer").tool_callable("calc"));
        assert!(!p("mcp_user").tool_listable("calc") && !p("mcp_user").tool_callable("calc"));
        assert!(!p("key").tool_listable("hidden"));
        assert!(p("mcp_user").tool_listable("other") && p("mcp_user").tool_callable("other"));
        assert!(p("super_admin").tool_listable("hidden") && p("super_admin").tool_callable("calc"));
        // the log reason: hidden beats denied; nothing for an unrestricted tool or another method
        assert_eq!(
            p("mcp_user").denial("tools/call", &json!({"name": "calc"})),
            Some("tool_hidden")
        );
        assert_eq!(
            p("viewer").denial("tools/call", &json!({"name": "calc"})),
            Some("tool_denied")
        );
        assert_eq!(
            p("key").denial("tools/call", &json!({"name": "calc"})),
            None
        );
        assert_eq!(
            p("viewer").denial("tools/call", &json!({"name": "other"})),
            None
        );
        assert_eq!(
            p("mcp_user").denial("prompts/get", &json!({"name": "calc"})),
            None
        );
        assert_eq!(p("mcp_user").denial("tools/call", &json!({})), None);
    }

    /// §21.4 (0.7.5): a custom role matches a list that names it or its base; an unknown kind with no
    /// `RAMEN_ROLES` entry still matches nothing.
    #[test]
    fn custom_roles_match_by_name_or_base() {
        let m = access(
            r#"{"calc":{"list":["viewer"],"call":["analyst"]},"ops_only":{"list":["ops"],"call":["ops"]}}"#,
        );
        let roles: HashMap<String, String> = [
            ("analyst".to_string(), "viewer".to_string()),
            ("ops".to_string(), "group_admin".to_string()),
        ]
        .into();
        let p = |kind: &'static str| Policy {
            blocked: &[],
            tool_access: &m,
            kind,
            roles: &roles,
        };
        // analyst: listable via its base (viewer), callable by name
        assert!(p("analyst").tool_listable("calc") && p("analyst").tool_callable("calc"));
        // a plain viewer may list but not call; group_admin is not named anywhere on calc
        assert!(p("viewer").tool_listable("calc") && !p("viewer").tool_callable("calc"));
        assert!(!p("group_admin").tool_listable("calc"));
        // ops is named directly; its base (group_admin) is not, so a plain group_admin sees nothing
        assert!(p("ops").tool_listable("ops_only") && p("ops").tool_callable("ops_only"));
        assert!(!p("group_admin").tool_listable("ops_only"));
        // an unknown role with no RAMEN_ROLES entry fails closed, as in 0.7.2
        assert!(!p("stranger").tool_listable("calc") && !p("stranger").tool_callable("calc"));
        assert_eq!(
            p("stranger").denial("tools/call", &json!({"name": "calc"})),
            Some("tool_hidden")
        );
        assert_eq!(
            p("analyst").denial("tools/call", &json!({"name": "calc"})),
            None
        );
    }

    #[tokio::test]
    async fn tools_list_is_filtered_per_kind_and_calls_are_hidden_or_forbidden() {
        let sc = sidecar();
        sc.set_loaded_for_test(
            json!({"tools": [tool("calc", scalar_schema()), tool("open", scalar_schema())],
            "prompts": [{"name": "calc"}]}),
        )
        .await;
        let m = access(r#"{"calc":{"list":["key","viewer"],"call":["key"]}}"#);
        let list = |kind: &'static str| {
            let sc = &sc;
            let p = Policy {
                blocked: &[],
                tool_access: &m,
                roles: no_roles(),
                kind,
            };
            async move {
                dispatch(sc, &p, "tools/list", &json!({}))
                    .await
                    .unwrap()
                    .unwrap()["tools"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|t| t["name"].as_str().unwrap().to_string())
                    .collect::<Vec<_>>()
            }
        };
        assert_eq!(list("key").await, vec!["calc", "open"]);
        assert_eq!(list("viewer").await, vec!["calc", "open"]);
        assert_eq!(list("mcp_user").await, vec!["open"]);
        assert_eq!(list("super_admin").await, vec!["calc", "open"]);
        let call = json!({"name": "calc", "arguments": {"a": 1, "b": "x"}});
        let p = Policy {
            blocked: &[],
            tool_access: &m,
            roles: no_roles(),
            kind: "mcp_user",
        };
        let e = dispatch(&sc, &p, "tools/call", &call).await.unwrap_err();
        assert_eq!(
            (e.code, e.message.as_str()),
            (-32601, "tool not found: calc")
        );
        let p = Policy {
            blocked: &[],
            tool_access: &m,
            roles: no_roles(),
            kind: "viewer",
        };
        let e = dispatch(&sc, &p, "tools/call", &call).await.unwrap_err();
        assert_eq!(
            (e.code, e.message.as_str()),
            (-32003, "forbidden: calc is not callable for viewer")
        );
        // prompts are not covered in 0.7.2: a prompt that shares the name stays listed
        let p = Policy {
            blocked: &[],
            tool_access: &m,
            roles: no_roles(),
            kind: "mcp_user",
        };
        assert_eq!(
            dispatch(&sc, &p, "prompts/list", &json!({}))
                .await
                .unwrap()
                .unwrap()["prompts"][0]["name"],
            "calc"
        );
        // C11: the node now tells clients it will announce list changes
        let init = dispatch(&sc, &p, "initialize", &json!({}))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(init["capabilities"]["tools"]["listChanged"], true);
    }

    /// §23.3: `{var}` is one non-empty path segment.
    #[test]
    fn templates_match_one_segment_per_variable() {
        let t = "test://template/{id}/data";
        assert!(template_matches(t, "test://template/123/data"));
        assert!(template_matches(t, "test://template/a-b.c/data"));
        assert!(!template_matches(t, "test://template//data"));
        assert!(!template_matches(t, "test://template/1/2/data"));
        assert!(!template_matches(t, "test://template/123/data/more"));
        assert!(!template_matches(t, "test://other/123/data"));
        assert!(template_matches("ramen://{a}/{b}", "ramen://x/y"));
        assert!(!template_matches("ramen://{a}/{b}", "ramen://x"));
        assert!(template_matches("ramen://v/{id}.json", "ramen://v/7.json"));
        assert!(template_matches(
            "ramen://v/{id}",
            "ramen://v/\u{e9}t\u{e9}"
        ));
        assert!(template_matches("plain", "plain") && !template_matches("plain", "plain2"));
        assert!(!template_matches("ramen://{broken", "ramen://x"));
    }

    /// §23.5 / §23.1: how the load result marks a task tool, and where a declared `enum` is read from.
    #[test]
    fn task_tools_and_declared_enums_are_read_from_the_load_result() {
        assert!(is_task_tool(&json!({"name": "t", "task": true})));
        assert!(is_task_tool(
            &json!({"name": "t", "execution": {"taskSupport": "optional"}})
        ));
        assert!(is_task_tool(
            &json!({"name": "t", "_meta": {"io.modelcontextprotocol/tasks": {}}})
        ));
        for plain in [
            json!({"name": "t"}),
            json!({"name": "t", "task": false}),
            json!({"name": "t", "task": "yes"}),
            json!({"name": "t", "execution": {"taskSupport": "forbidden"}}),
        ] {
            assert!(!is_task_tool(&plain), "{plain}");
        }
        let prompt = json!({"arguments": [{"name": "tone", "enum": ["a", "b"]}, {"name": "who"}]});
        assert_eq!(declared_enum(&prompt, "tone").map(Vec::len), Some(2));
        assert!(declared_enum(&prompt, "who").is_none() && declared_enum(&prompt, "x").is_none());
        let template = json!({"uriTemplate": "r://{id}", "variables": {"id": {"enum": ["1"]}}});
        assert_eq!(declared_enum(&template, "id").map(Vec::len), Some(1));
        assert!(declared_enum(&json!({}), "id").is_none());
    }

    /// The legacy wrapper has no host: a runtime that asks for input is answered the 2025-06-18 way, and the
    /// methods of 2026-07-28 do not exist.
    #[tokio::test]
    async fn the_legacy_wrapper_serves_no_modern_method() {
        let sc = sidecar();
        sc.set_loaded_for_test(json!({"tools": [], "prompts": []}))
            .await;
        let p = Policy {
            blocked: &[],
            tool_access: &HashMap::new(),
            roles: no_roles(),
            kind: "key",
        };
        for m in ["server/discover", "tasks/get", "subscriptions/listen"] {
            assert_eq!(
                dispatch(&sc, &p, m, &json!({})).await.unwrap_err().code,
                -32601,
                "{m}"
            );
        }
        // templates: none reported by a 0.8 runtime is an empty list, not null
        assert_eq!(
            dispatch(&sc, &p, "resources/templates/list", &json!({}))
                .await
                .unwrap(),
            Some(json!({"resourceTemplates": []}))
        );
        let done =
            |r: Value| match round_result(&Env::legacy(&sc), "tools/call", "t", &json!({}), r) {
                Ok(Round::Legacy(msg)) => msg,
                _ => String::new(),
            };
        assert_eq!(
            done(json!({"resultType": "input_required", "inputRequests": {"a": {}, "b": {}}})),
            "this tool needs more input (a, b); it can only ask a client that speaks MCP 2026-07-28"
        );
        assert!(
            done(json!({"resultType": "missing_capability", "requiredCapabilities": {"sampling": {}}}))
                .starts_with("this tool needs a client capability (sampling)")
        );
    }

    #[tokio::test]
    async fn invalid_tool_args_is_none_for_an_unknown_tool() {
        let sc = sidecar();
        sc.set_loaded_for_test(json!({"tools": [tool("known", scalar_schema())]}))
            .await;
        assert!(
            invalid_tool_args(&sc, "unknown", &json!({}))
                .await
                .is_none()
        );
    }

    #[tokio::test]
    async fn invalid_tool_args_validates_against_the_matching_tools_schema() {
        let sc = sidecar();
        sc.set_loaded_for_test(json!({"tools": [tool("known", scalar_schema())]}))
            .await;
        assert!(
            invalid_tool_args(&sc, "known", &json!({"a": 1, "b": "x"}))
                .await
                .is_none()
        );
        assert!(
            invalid_tool_args(&sc, "known", &json!({"a": 1}))
                .await
                .is_some()
        );
    }

    #[tokio::test]
    async fn missing_prompt_args_checks_presence_only() {
        let sc = sidecar();
        sc.set_loaded_for_test(json!({"prompts": [
            {"name": "p", "arguments": [{"name": "x", "required": true}, {"name": "y", "required": true}]}
        ]}))
        .await;
        assert!(
            missing_prompt_args(&sc, "p", &json!({"x": 1, "y": 2}))
                .await
                .is_none()
        );
        let msg = missing_prompt_args(&sc, "p", &json!({"x": 1}))
            .await
            .unwrap();
        assert!(msg.contains('y'), "{msg}");
        assert!(
            missing_prompt_args(&sc, "unknown", &json!({}))
                .await
                .is_none()
        );
    }
}
