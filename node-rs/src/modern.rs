//! MCP 2026-07-28 — the "modern" era (CONTRACTS §23.1, §23.2, §23.4).
//!
//! Pure functions only: era selection, the per-request `_meta` envelope, the standard headers, the result envelope,
//! the modern error codes and the signed `requestState`. `grpc::dispatch_call` runs them in the contract's order for
//! both transports; nothing here touches a guard, and nothing here is reached by a 2025-06-18 client.
use crate::sidecar::RpcErr;
use base64::Engine;
use base64::engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD as B64URL};
use http::HeaderMap;
use ring::hmac;
use serde_json::{Map, Value, json};

pub const MODERN: &str = "2026-07-28";
/// Newest first; what `http::PROTOCOL_VERSIONS` has always been.
pub const LEGACY: &[&str] = &["2025-06-18", "2025-03-26", "2024-11-05"];
pub const META_VERSION: &str = "io.modelcontextprotocol/protocolVersion";
pub const META_CAPS: &str = "io.modelcontextprotocol/clientCapabilities";
pub const META_SERVER: &str = "io.modelcontextprotocol/serverInfo";
pub const META_SUBSCRIPTION: &str = "io.modelcontextprotocol/subscriptionId";
pub const TASKS_EXT: &str = "io.modelcontextprotocol/tasks";
pub const PROTOCOL_HEADER: &str = "mcp-protocol-version";

/// Access-log reasons this era adds (C6 style: a short code, never a value).
pub const HEADER_MISMATCH: &str = "header_mismatch";
pub const STATE_INVALID: &str = "state_invalid";

/// `[MODERN, ...LEGACY]`: `server/discover` and the `-32022` data.
pub fn supported() -> Vec<&'static str> {
    std::iter::once(MODERN)
        .chain(LEGACY.iter().copied())
        .collect()
}

/// A header's value as text, trimmed. `Some(Err(()))` when present but not visible ASCII.
pub fn header<'a>(headers: &'a HeaderMap, name: &str) -> Option<Result<&'a str, ()>> {
    headers
        .get(name)
        .map(|v| v.to_str().map(str::trim).map_err(|_| ()))
}

/// §23.1: modern when the HTTP `MCP-Protocol-Version` header is not a legacy version, or the body carries
/// `params._meta[protocolVersion]`. `headers` is `None` on gRPC, where only the body decides.
pub fn is_modern(headers: Option<&HeaderMap>, params: &Value) -> bool {
    let by_header = headers
        .and_then(|h| header(h, PROTOCOL_HEADER))
        .is_some_and(|v| !v.is_ok_and(|v| LEGACY.contains(&v)));
    by_header
        || params
            .get("_meta")
            .and_then(|m| m.get(META_VERSION))
            .is_some()
}

/// A modern check that failed: the JSON-RPC error and the HTTP status it travels with (§23.1).
#[derive(Debug, PartialEq)]
pub struct Refusal {
    pub err: RpcErr,
    pub status: u16,
}

fn mismatch(what: &str) -> Refusal {
    Refusal {
        err: RpcErr::new(-32020, format!("Header mismatch: {what}")).because(HEADER_MISMATCH),
        status: 400,
    }
}

/// Modern checks 1–3, in the contract's order: `_meta` → header = body version → the version is ours. Returns the
/// declared client capabilities.
pub fn envelope<'a>(headers: Option<&HeaderMap>, params: &'a Value) -> Result<&'a Value, Refusal> {
    let meta = params.get("_meta").and_then(Value::as_object);
    let version = meta
        .and_then(|m| m.get(META_VERSION))
        .and_then(Value::as_str);
    let caps = meta
        .and_then(|m| m.get(META_CAPS))
        .filter(|c| c.is_object());
    let (Some(version), Some(caps)) = (version, caps) else {
        return Err(Refusal {
            err: RpcErr::new(
                -32602,
                format!("invalid params: _meta must carry {META_VERSION} and {META_CAPS}"),
            ),
            status: 400,
        });
    };
    if let Some(h) = headers {
        match header(h, PROTOCOL_HEADER) {
            Some(Ok(v)) if v == version => {}
            Some(_) => {
                return Err(mismatch(
                    "MCP-Protocol-Version does not match the version in _meta",
                ));
            }
            None => return Err(mismatch("MCP-Protocol-Version is required")),
        }
    }
    if version != MODERN {
        // the requested string goes back to the caller that sent it, bounded so a hostile one stays small
        let requested: String = version.chars().take(64).collect();
        return Err(Refusal {
            err: RpcErr::new(-32022, "Unsupported protocol version")
                .with_data(json!({"supported": supported(), "requested": requested})),
            status: 400,
        });
    }
    Ok(caps)
}

/// A `=?base64?…?=` header value that is not Base64 of UTF-8.
#[derive(Debug, PartialEq)]
pub struct Undecodable;

/// A `requestState` that is not ours, not fresh, or not for this caller and request.
#[derive(Debug, PartialEq)]
pub struct BadState;

/// §23.2: `=?base64?<b64>?=` is standard Base64 of UTF-8 text; anything else is the value as sent.
pub fn decode_value(raw: &str) -> Result<String, Undecodable> {
    match raw
        .strip_prefix("=?base64?")
        .and_then(|r| r.strip_suffix("?="))
    {
        Some(b64) => STANDARD
            .decode(b64)
            .ok()
            .and_then(|b| String::from_utf8(b).ok())
            .ok_or(Undecodable),
        None => Ok(raw.to_string()),
    }
}

/// The params field `Mcp-Name` mirrors, for the methods that need the header (§23.2).
pub fn name_field(method: &str) -> Option<&'static str> {
    match method {
        "tools/call" | "prompts/get" => Some("name"),
        "resources/read" => Some("uri"),
        "tasks/get" | "tasks/update" | "tasks/cancel" => Some("taskId"),
        _ => None,
    }
}

/// Modern check 4 (HTTP): `Mcp-Method`, `Mcp-Name`, and — for a `tools/call` on a tool the caller may see —
/// `Mcp-Param-<Name>` for every argument the tool's `inputSchema` annotates with `x-mcp-header`. The body is
/// authoritative; a header that disagrees is refused, never used.
pub fn standard_headers(
    headers: &HeaderMap,
    method: &str,
    params: &Value,
    tool: Option<&Value>,
) -> Result<(), Refusal> {
    match header(headers, "mcp-method") {
        Some(Ok(v)) if v == method => {}
        Some(_) => return Err(mismatch("Mcp-Method does not match the body")),
        None => return Err(mismatch("Mcp-Method is required")),
    }
    if let Some(field) = name_field(method) {
        let sent = match header(headers, "mcp-name") {
            Some(Ok(v)) => decode_value(v).map_err(|_| mismatch("Mcp-Name is not decodable"))?,
            Some(Err(())) => return Err(mismatch("Mcp-Name is not decodable")),
            None => return Err(mismatch("Mcp-Name is required")),
        };
        // a body without the field is the dispatcher's `-32602`; only a present one can disagree
        if let Some(body) = params.get(field).and_then(Value::as_str)
            && body != sent
        {
            return Err(mismatch("Mcp-Name does not match the body"));
        }
    }
    let annotated = tool
        .filter(|_| method == "tools/call")
        .and_then(|t| t.pointer("/inputSchema/properties"))
        .and_then(Value::as_object);
    for (prop, schema) in annotated.into_iter().flatten() {
        let Some(name) = schema.get("x-mcp-header").and_then(Value::as_str) else {
            continue;
        };
        let arg = params.get("arguments").and_then(|a| a.get(prop));
        let Some(arg) = arg.filter(|a| !a.is_null()) else {
            continue; // absent or null: the header is not expected
        };
        let key = format!("mcp-param-{}", name.to_ascii_lowercase());
        let sent = match header(headers, &key) {
            Some(Ok(v)) => decode_value(v)
                .map_err(|_| mismatch(&format!("Mcp-Param-{name} is not decodable")))?,
            Some(Err(())) => return Err(mismatch(&format!("Mcp-Param-{name} is not decodable"))),
            None => return Err(mismatch(&format!("Mcp-Param-{name} is required"))),
        };
        if !param_matches(arg, &sent) {
            return Err(mismatch(&format!(
                "Mcp-Param-{name} does not match the body"
            )));
        }
    }
    Ok(())
}

/// Strings exactly, integers numerically, booleans as `true` / `false`.
fn param_matches(arg: &Value, sent: &str) -> bool {
    match arg {
        Value::String(s) => s == sent,
        Value::Bool(b) => sent == if *b { "true" } else { "false" },
        Value::Number(n) => match (n.as_i64(), n.as_u64()) {
            (Some(i), _) => sent.parse::<i64>().ok() == Some(i),
            (None, Some(u)) => sent.parse::<u64>().ok() == Some(u),
            _ => false, // `number` is not annotatable (§23.2)
        },
        _ => false,
    }
}

/// Modern check 5: what the node serves on this era. `initialize`, `ping`, `logging/setLevel`,
/// `resources/subscribe|unsubscribe` are gone; `subscriptions/listen` is HTTP only (§23.6).
pub fn known_method(method: &str, http: bool) -> bool {
    matches!(
        method,
        "server/discover"
            | "tools/list"
            | "tools/call"
            | "resources/list"
            | "resources/templates/list"
            | "resources/read"
            | "prompts/list"
            | "prompts/get"
            | "completion/complete"
            | "tasks/get"
            | "tasks/update"
            | "tasks/cancel"
    ) || (http && method == "subscriptions/listen")
}

/// §23.1: the HTTP status of a modern JSON-RPC error raised by dispatch (the `_meta` `-32602` is a `Refusal`).
pub fn http_status(code: i64) -> u16 {
    match code {
        -32022..=-32020 => 400,
        -32601 => 404,
        _ => 200,
    }
}

pub fn server_info() -> Value {
    json!({"name": "ramen-node", "version": env!("CARGO_PKG_VERSION")})
}

/// §23.1: every modern result says what it is and who answered; the cacheable ones say for how long.
pub fn decorate(method: &str, mut result: Value, list_ttl_ms: u64) -> Value {
    let Some(o) = result.as_object_mut() else {
        return result;
    };
    if !o.contains_key("resultType") {
        o.insert("resultType".into(), json!("complete"));
    }
    match o.entry("_meta").or_insert_with(|| json!({})) {
        Value::Object(m) => {
            m.insert(META_SERVER.into(), server_info());
        }
        other => *other = json!({ META_SERVER: server_info() }),
    }
    let ttl = match method {
        "resources/read" => Some(0),
        "tools/list"
        | "prompts/list"
        | "resources/list"
        | "resources/templates/list"
        | "server/discover" => Some(list_ttl_ms),
        _ => None,
    };
    if let Some(ttl) = ttl.filter(|_| o["resultType"] == "complete") {
        o.insert("ttlMs".into(), json!(ttl));
        o.insert("cacheScope".into(), json!("private")); // lists vary by caller (C10)
    }
    result
}

/// §23.1: nothing from `-32000..-32019` leaves on this era, and "not there" is one answer whether the name is
/// unknown, blocked or hidden.
pub fn map_error(method: &str, params: &Value, e: RpcErr) -> RpcErr {
    let name = |k: &str| params.get(k).and_then(Value::as_str).unwrap_or("");
    let not_found = matches!(e.code, -32601 | -32004);
    match (method, e.code) {
        ("tools/call", _) if not_found => {
            RpcErr::new(-32602, format!("Unknown tool: {}", name("name")))
        }
        ("prompts/get", _) if not_found => {
            RpcErr::new(-32602, format!("Unknown prompt: {}", name("name")))
        }
        ("resources/read", _) if not_found => {
            RpcErr::new(-32602, "Resource not found").with_data(json!({"uri": name("uri")}))
        }
        (_, -32003) => RpcErr { code: -32602, ..e },
        // -32601 past check 5 is the runtime's, not the client's
        (_, -32019..=-32000 | -32601) => RpcErr { code: -32603, ..e },
        _ => e,
    }
}

/// §23.2: `traceparent` (+ `tracestate`, `baggage`) from the HTTP header, else the `_meta` key — for the access
/// log only, and only when `traceparent` has the W3C shape. Never forwarded to tools.
pub fn trace(headers: Option<&HeaderMap>, params: &Value) -> Option<Value> {
    let get = |k: &str| -> Option<String> {
        headers
            .and_then(|h| header(h, k))
            .and_then(Result::ok)
            .or_else(|| {
                params
                    .get("_meta")
                    .and_then(|m| m.get(k))
                    .and_then(Value::as_str)
            })
            .map(|s| s.chars().take(512).collect())
    };
    let traceparent = get("traceparent").filter(|t| is_traceparent(t))?;
    let mut out = Map::new();
    out.insert("traceparent".into(), json!(traceparent));
    for k in ["tracestate", "baggage"] {
        if let Some(v) = get(k) {
            out.insert(k.into(), json!(v));
        }
    }
    Some(Value::Object(out))
}

/// `<2 hex>-<32 hex>-<16 hex>-<2 hex>`, lowercase.
fn is_traceparent(t: &str) -> bool {
    let parts: Vec<&str> = t.split('-').collect();
    let hex = |s: &str, n: usize| {
        s.len() == n
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    };
    matches!(parts[..], [v, tid, sid, f] if hex(v, 2) && hex(tid, 32) && hex(sid, 16) && hex(f, 2))
}

/// Whether the client declared the tasks extension on this request (§23.5).
pub fn tasks_declared(caps: &Value) -> bool {
    caps.pointer("/extensions")
        .and_then(|e| e.get(TASKS_EXT))
        .is_some_and(Value::is_object)
}

pub fn tasks_required() -> RpcErr {
    missing_capability(json!({"extensions": {TASKS_EXT: {}}}))
}

/// `-32021` with `data.requiredCapabilities` (an object, like `ClientCapabilities`).
pub fn missing_capability(required: Value) -> RpcErr {
    let names: Vec<&str> = required
        .as_object()
        .map(|o| o.keys().map(String::as_str).collect())
        .unwrap_or_default();
    RpcErr::new(
        -32021,
        format!("missing required client capability: {}", names.join(", ")),
    )
    .with_data(json!({"requiredCapabilities": required}))
}

/// §23.4: the capabilities a set of input requests needs that `caps` does not declare — `None` when all are
/// declared. `Err` when an entry is not one of the three request kinds MRTR allows.
pub fn undeclared(requests: &Value, caps: &Value) -> Result<Option<Value>, RpcErr> {
    let bad =
        || RpcErr::internal("the tool asked for an input request the protocol does not define");
    let has = |k: &str| caps.get(k).is_some_and(Value::is_object);
    let mut need = Map::new();
    for req in requests.as_object().ok_or_else(bad)?.values() {
        match req.get("method").and_then(Value::as_str).ok_or_else(bad)? {
            "elicitation/create" => {
                let url = req.pointer("/params/mode").and_then(Value::as_str) == Some("url");
                if url && caps.pointer("/elicitation/url").is_none() {
                    need.insert("elicitation".into(), json!({"url": {}}));
                } else if !has("elicitation") {
                    need.entry("elicitation").or_insert_with(|| json!({}));
                }
            }
            "sampling/createMessage" if !has("sampling") => {
                need.insert("sampling".into(), json!({}));
            }
            "roots/list" if !has("roots") => {
                need.insert("roots".into(), json!({}));
            }
            "sampling/createMessage" | "roots/list" => {}
            _ => return Err(bad()),
        }
    }
    Ok((!need.is_empty()).then_some(Value::Object(need)))
}

/// What a `requestState` is bound to (§23.4): the caller (§16.2 binding), this group and zone, and the request
/// it was issued for.
pub struct StateBinding<'a> {
    pub caller: &'a str,
    pub group: &'a str,
    pub zone: &'a str,
    pub method: &'a str,
    pub name: &'a str,
    /// C12 digest of the call's `arguments`.
    pub args: &'a str,
}

impl StateBinding<'_> {
    fn payload(&self, state: &Value, expiry: u64) -> Value {
        json!({"v": 1, "s": state, "b": self.caller, "g": self.group, "z": self.zone, "m": self.method,
               "n": self.name, "a": self.args, "e": expiry})
    }
}

/// `b64url(payload) "." b64url(HMAC-SHA256(key, payload))`.
pub fn sign_state(key: &hmac::Key, bind: &StateBinding, state: &Value, expiry: u64) -> String {
    let payload = bind.payload(state, expiry).to_string();
    let mac = hmac::sign(key, payload.as_bytes());
    format!("{}.{}", B64URL.encode(&payload), B64URL.encode(mac))
}

/// The tool's `state` when the token is ours, unexpired, and was issued for exactly this caller and request.
/// Every failure is the same `BadState`: the caller learns only that the state was refused.
pub fn verify_state(
    key: &hmac::Key,
    bind: &StateBinding,
    token: &str,
    now: u64,
) -> Result<Value, BadState> {
    let (p, m) = token.split_once('.').ok_or(BadState)?;
    let payload = B64URL.decode(p).map_err(|_| BadState)?;
    let mac = B64URL.decode(m).map_err(|_| BadState)?;
    hmac::verify(key, &payload, &mac).map_err(|_| BadState)?; // MAC first, as session.rs does
    let v: Value = serde_json::from_slice(&payload).map_err(|_| BadState)?;
    let expiry = v["e"].as_u64().ok_or(BadState)?;
    if expiry <= now || v != bind.payload(&v["s"], expiry) {
        return Err(BadState);
    }
    Ok(v["s"].clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn h(pairs: &[(&'static str, &str)]) -> HeaderMap {
        let mut m = HeaderMap::new();
        for (k, v) in pairs {
            m.insert(*k, http::HeaderValue::from_bytes(v.as_bytes()).unwrap());
        }
        m
    }

    fn meta(version: &str) -> Value {
        json!({"_meta": {META_VERSION: version, META_CAPS: {}}})
    }

    /// §23.1: a non-legacy header or a body version selects the modern era; everything else stays legacy.
    #[test]
    fn era_is_selected_by_header_or_body() {
        assert!(!is_modern(None, &json!({})));
        assert!(!is_modern(
            Some(&h(&[])),
            &json!({"_meta": {"progressToken": 1}})
        ));
        for legacy in LEGACY {
            assert!(!is_modern(
                Some(&h(&[(PROTOCOL_HEADER, legacy)])),
                &json!({})
            ));
        }
        assert!(is_modern(
            Some(&h(&[(PROTOCOL_HEADER, MODERN)])),
            &json!({})
        ));
        assert!(is_modern(
            Some(&h(&[(PROTOCOL_HEADER, "1999-01-01")])),
            &json!({})
        ));
        assert!(is_modern(
            Some(&h(&[(PROTOCOL_HEADER, "2025-11-25")])),
            &json!({})
        ));
        assert!(is_modern(
            Some(&h(&[(PROTOCOL_HEADER, "\u{e9}")])),
            &json!({})
        ));
        // gRPC: only the body decides
        assert!(is_modern(None, &meta(MODERN)));
        assert!(is_modern(
            Some(&h(&[(PROTOCOL_HEADER, "2025-06-18")])),
            &meta("x")
        ));
    }

    /// §23.1 checks 1–3 and their order: `_meta` (-32602) before the header (-32020) before the version (-32022).
    #[test]
    fn envelope_checks_run_in_the_contract_order() {
        let good = h(&[(PROTOCOL_HEADER, MODERN)]);
        fn code<'a>(hd: Option<&HeaderMap>, p: &'a Value) -> Result<&'a Value, (i64, u16)> {
            envelope(hd, p).map_err(|r| (r.err.code, r.status))
        }
        assert_eq!(code(Some(&good), &json!({})), Err((-32602, 400)));
        assert_eq!(
            code(Some(&good), &json!({"_meta": {META_CAPS: {}}})),
            Err((-32602, 400))
        );
        assert_eq!(
            code(Some(&good), &json!({"_meta": {META_VERSION: MODERN}})),
            Err((-32602, 400))
        );
        assert_eq!(
            code(
                Some(&good),
                &json!({"_meta": {META_VERSION: MODERN, META_CAPS: []}})
            ),
            Err((-32602, 400))
        );
        // clientInfo is optional
        assert_eq!(code(Some(&good), &meta(MODERN)), Ok(&json!({})));
        // header missing or different: -32020, before the version is judged
        let r = envelope(Some(&h(&[])), &meta(MODERN)).unwrap_err();
        assert_eq!(
            (r.err.code, r.status, r.err.reason),
            (-32020, 400, Some(HEADER_MISMATCH))
        );
        assert_eq!(code(Some(&good), &meta("v999.0.0")), Err((-32020, 400)));
        // both say the same unknown version: -32022 with what we speak and what was asked
        let r = envelope(
            Some(&h(&[(PROTOCOL_HEADER, "v999.0.0")])),
            &meta("v999.0.0"),
        )
        .unwrap_err();
        assert_eq!((r.err.code, r.status), (-32022, 400));
        assert_eq!(
            r.err.data,
            Some(
                json!({"supported": ["2026-07-28", "2025-06-18", "2025-03-26", "2024-11-05"], "requested": "v999.0.0"})
            )
        );
        // gRPC: no header rules, the body version alone
        assert_eq!(code(None, &meta(MODERN)), Ok(&json!({})));
        assert_eq!(code(None, &meta("2025-06-18")), Err((-32022, 400)));
    }

    /// §23.2: the sentinel is decoded strictly; a value that only looks half like one is literal.
    #[test]
    fn base64_sentinel() {
        assert_eq!(decode_value("=?base64?SGVsbG8=?=").as_deref(), Ok("Hello"));
        assert_eq!(
            decode_value("=?base64?SGVsbG8sIOS4lueVjA==?=").as_deref(),
            Ok("Hello, 世界")
        );
        assert_eq!(decode_value("=?base64?SGVsbG8?="), Err(Undecodable)); // padding
        assert_eq!(decode_value("=?base64?SGVs!!!bG8=?="), Err(Undecodable));
        assert_eq!(decode_value("=?base64?/w==?="), Err(Undecodable)); // not UTF-8
        assert_eq!(decode_value("SGVsbG8=").as_deref(), Ok("SGVsbG8="));
        assert_eq!(
            decode_value("=?base64?SGVsbG8=").as_deref(),
            Ok("=?base64?SGVsbG8=")
        );
        assert_eq!(
            decode_value("=?BASE64?SGVsbG8=?=").as_deref(),
            Ok("=?BASE64?SGVsbG8=?=")
        );
    }

    fn tool() -> Value {
        json!({"name": "sql", "inputSchema": {"type": "object", "properties": {
            "region": {"type": "string", "x-mcp-header": "Region"},
            "limit": {"type": "integer", "x-mcp-header": "Limit"},
            "dry": {"type": "boolean", "x-mcp-header": "Dry-Run"},
            "query": {"type": "string"}}}})
    }

    #[test]
    fn mcp_method_and_name_must_match_the_body() {
        let ok = |hd: &[(&'static str, &str)], m: &str, p: Value| {
            standard_headers(&h(hd), m, &p, None).map_err(|r| (r.err.code, r.status))
        };
        assert_eq!(
            ok(&[("mcp-method", "tools/list")], "tools/list", json!({})),
            Ok(())
        );
        assert_eq!(ok(&[], "tools/list", json!({})), Err((-32020, 400)));
        assert_eq!(
            ok(&[("mcp-method", "prompts/list")], "tools/list", json!({})),
            Err((-32020, 400))
        );
        assert_eq!(
            ok(&[("mcp-method", "TOOLS/LIST")], "tools/list", json!({})),
            Err((-32020, 400))
        );
        // Mcp-Name: required where the method has a target, trimmed, sentinel-decoded
        let call = json!({"name": "sql"});
        assert_eq!(
            ok(&[("mcp-method", "tools/call")], "tools/call", call.clone()),
            Err((-32020, 400))
        );
        for good in ["sql", "  sql  ", "=?base64?c3Fs?="] {
            assert_eq!(
                ok(
                    &[("mcp-method", "tools/call"), ("mcp-name", good)],
                    "tools/call",
                    call.clone()
                ),
                Ok(()),
                "{good}"
            );
        }
        for bad in ["other", "SQL", "=?base64?c3F?=", "\u{e9}"] {
            assert_eq!(
                ok(
                    &[("mcp-method", "tools/call"), ("mcp-name", bad)],
                    "tools/call",
                    call.clone()
                ),
                Err((-32020, 400)),
                "{bad}"
            );
        }
        assert_eq!(
            ok(
                &[
                    ("mcp-method", "resources/read"),
                    ("mcp-name", "ramen://a/b")
                ],
                "resources/read",
                json!({"uri": "ramen://a/b"})
            ),
            Ok(())
        );
        assert_eq!(
            ok(
                &[("mcp-method", "tasks/get"), ("mcp-name", "wrong")],
                "tasks/get",
                json!({"taskId": "abc"})
            ),
            Err((-32020, 400))
        );
        assert_eq!(
            ok(
                &[("mcp-method", "tasks/cancel"), ("mcp-name", "abc")],
                "tasks/cancel",
                json!({"taskId": "abc"})
            ),
            Ok(())
        );
    }

    /// §23.2: present and non-null ⇒ header required and equal; absent or null ⇒ not expected; unknown ignored.
    #[test]
    fn mcp_param_headers_follow_the_body() {
        let base = [("mcp-method", "tools/call"), ("mcp-name", "sql")];
        let check = |extra: &[(&'static str, &str)], args: Value| {
            let mut hd = base.to_vec();
            hd.extend_from_slice(extra);
            standard_headers(
                &h(&hd),
                "tools/call",
                &json!({"name": "sql", "arguments": args}),
                Some(&tool()),
            )
            .map_err(|r| r.err.code)
        };
        assert_eq!(check(&[], json!({"query": "select 1"})), Ok(()));
        assert_eq!(check(&[], json!({"region": null})), Ok(()));
        assert_eq!(
            check(&[("mcp-param-region", "us")], json!({"region": "us"})),
            Ok(())
        );
        assert_eq!(
            check(&[("MCP-PARAM-REGION", "us")], json!({"region": "us"})),
            Ok(())
        );
        assert_eq!(check(&[], json!({"region": "us"})), Err(-32020));
        assert_eq!(
            check(&[("mcp-param-region", "eu")], json!({"region": "us"})),
            Err(-32020)
        );
        assert_eq!(
            check(
                &[("mcp-param-region", "=?base64?SGVsbG8=?=")],
                json!({"region": "Hello"})
            ),
            Ok(())
        );
        assert_eq!(
            check(
                &[("mcp-param-region", "=?base64?SGVsbG8?=")],
                json!({"region": "Hello"})
            ),
            Err(-32020)
        );
        // integers numerically, booleans as words
        assert_eq!(
            check(&[("mcp-param-limit", "042")], json!({"limit": 42})),
            Ok(())
        );
        assert_eq!(
            check(&[("mcp-param-limit", "-7")], json!({"limit": -7})),
            Ok(())
        );
        assert_eq!(
            check(&[("mcp-param-limit", "43")], json!({"limit": 42})),
            Err(-32020)
        );
        assert_eq!(
            check(&[("mcp-param-limit", "x")], json!({"limit": 42})),
            Err(-32020)
        );
        assert_eq!(
            check(&[("mcp-param-dry-run", "true")], json!({"dry": true})),
            Ok(())
        );
        assert_eq!(
            check(&[("mcp-param-dry-run", "True")], json!({"dry": true})),
            Err(-32020)
        );
        assert_eq!(
            check(&[("mcp-param-dry-run", "false")], json!({"dry": false})),
            Ok(())
        );
        assert_eq!(
            check(&[("mcp-param-dry-run", "1")], json!({"dry": [1]})),
            Err(-32020)
        );
        // a header for a parameter the body does not carry, or that no tool declares, is ignored
        assert_eq!(
            check(
                &[("mcp-param-region", "us"), ("mcp-param-other", "x")],
                json!({})
            ),
            Ok(())
        );
        // no tool (unknown, blocked or hidden): nothing to compare, nothing leaked
        assert!(
            standard_headers(
                &h(&base),
                "tools/call",
                &json!({"name": "sql", "arguments": {"region": "us"}}),
                None
            )
            .is_ok()
        );
    }

    #[test]
    fn method_table_and_statuses() {
        for gone in [
            "initialize",
            "ping",
            "logging/setLevel",
            "resources/subscribe",
            "resources/unsubscribe",
            "tasks/list",
            "tasks/result",
            "unknown/method",
        ] {
            assert!(!known_method(gone, true), "{gone}");
        }
        assert!(known_method("server/discover", false) && known_method("tasks/get", false));
        assert!(known_method("subscriptions/listen", true));
        assert!(!known_method("subscriptions/listen", false)); // §23.6: -32601 over gRPC
        assert_eq!(
            [-32020, -32021, -32022, -32601, -32602, -32603].map(http_status),
            [400, 400, 400, 404, 200, 200]
        );
    }

    #[test]
    fn results_get_the_envelope() {
        let r = decorate("tools/call", json!({"content": [], "isError": false}), 5);
        assert_eq!(r["resultType"], "complete");
        assert_eq!(r["_meta"][META_SERVER]["name"], "ramen-node");
        assert_eq!(
            r["_meta"][META_SERVER]["version"],
            env!("CARGO_PKG_VERSION")
        );
        assert!(r.get("ttlMs").is_none() && r.get("cacheScope").is_none());
        // an existing _meta is merged into, not replaced
        let r = decorate(
            "tools/call",
            json!({"_meta": {"ramen": {"guardrail": {"stage": "pre"}}}}),
            5,
        );
        assert_eq!(r["_meta"]["ramen"]["guardrail"]["stage"], "pre");
        assert!(r["_meta"][META_SERVER].is_object());
        for m in [
            "tools/list",
            "prompts/list",
            "resources/list",
            "resources/templates/list",
            "server/discover",
        ] {
            let r = decorate(m, json!({}), 60_000);
            assert_eq!(
                (r["ttlMs"].as_u64(), r["cacheScope"].as_str()),
                (Some(60_000), Some("private")),
                "{m}"
            );
        }
        assert_eq!(decorate("resources/read", json!({}), 60_000)["ttlMs"], 0);
        // an interim result keeps its type and is never cacheable
        let r = decorate("tools/call", json!({"resultType": "input_required"}), 5);
        assert_eq!(r["resultType"], "input_required");
        let r = decorate("resources/read", json!({"resultType": "input_required"}), 5);
        assert!(r.get("ttlMs").is_none());
        assert_eq!(
            decorate("tools/call", json!({"resultType": "task"}), 5)["resultType"],
            "task"
        );
    }

    #[test]
    fn modern_errors_never_use_the_legacy_range() {
        let m = |method: &str, p: Value, code: i64, msg: &str| {
            let e = map_error(method, &p, RpcErr::new(code, msg));
            (e.code, e.message, e.data)
        };
        for code in [-32601, -32004] {
            assert_eq!(
                m(
                    "tools/call",
                    json!({"name": "calc"}),
                    code,
                    "tool not found: calc"
                ),
                (-32602, "Unknown tool: calc".into(), None)
            );
            assert_eq!(
                m("prompts/get", json!({"name": "p"}), code, "x"),
                (-32602, "Unknown prompt: p".into(), None)
            );
            assert_eq!(
                m("resources/read", json!({"uri": "ramen://x"}), code, "x"),
                (
                    -32602,
                    "Resource not found".into(),
                    Some(json!({"uri": "ramen://x"}))
                )
            );
        }
        assert_eq!(
            m(
                "tools/call",
                json!({"name": "calc"}),
                -32003,
                "forbidden: calc is not callable for viewer"
            ),
            (
                -32602,
                "forbidden: calc is not callable for viewer".into(),
                None
            )
        );
        assert_eq!(
            m("tools/list", json!({}), -32002, "runtime not loaded").0,
            -32603
        );
        assert_eq!(
            m(
                "tasks/get",
                json!({}),
                -32601,
                "method not found: runtime.task_poll"
            )
            .0,
            -32603
        );
        assert_eq!(
            m("tools/call", json!({}), -32602, "name must be a string").0,
            -32602
        );
        assert_eq!(m("tools/call", json!({}), -32021, "x").0, -32021);
        for code in -32019..=-32000 {
            let got = m("tools/list", json!({}), code, "x").0;
            assert!(!(-32019..=-32000).contains(&got), "{code} → {got}");
        }
    }

    #[test]
    fn trace_context_needs_a_well_formed_traceparent() {
        const TP: &str = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01";
        assert_eq!(
            trace(
                Some(&h(&[
                    ("traceparent", TP),
                    ("tracestate", "a=b"),
                    ("baggage", "k=v")
                ])),
                &json!({})
            ),
            Some(json!({"traceparent": TP, "tracestate": "a=b", "baggage": "k=v"}))
        );
        // the header wins over _meta; _meta alone works (gRPC)
        assert_eq!(
            trace(
                Some(&h(&[("traceparent", TP)])),
                &json!({"_meta": {"traceparent": "junk", "baggage": "m=1"}})
            ),
            Some(json!({"traceparent": TP, "baggage": "m=1"}))
        );
        assert_eq!(
            trace(None, &json!({"_meta": {"traceparent": TP}})),
            Some(json!({"traceparent": TP}))
        );
        for bad in [
            "",
            "junk",
            "00-0AF7651916CD43DD8448EB211C80319C-b7ad6b7169203331-01",
            "00-0af7-b7ad-01",
        ] {
            assert_eq!(
                trace(
                    Some(&h(&[("traceparent", bad), ("baggage", "k=v")])),
                    &json!({})
                ),
                None,
                "{bad}"
            );
        }
        assert_eq!(trace(None, &json!({})), None);
    }

    #[test]
    fn input_requests_need_declared_capabilities() {
        let elicit = json!({"method": "elicitation/create", "params": {"message": "m", "requestedSchema": {}}});
        let url = json!({"method": "elicitation/create", "params": {"mode": "url", "url": "https://x", "message": "m"}});
        let sample = json!({"method": "sampling/createMessage", "params": {}});
        let roots = json!({"method": "roots/list"});
        let all = json!({"a": elicit, "b": url, "c": sample, "d": roots});
        assert_eq!(
            undeclared(
                &all,
                &json!({"elicitation": {"form": {}, "url": {}}, "sampling": {}, "roots": {"listChanged": true}})
            ),
            Ok(None)
        );
        assert_eq!(
            undeclared(&all, &json!({})),
            Ok(Some(
                json!({"elicitation": {"url": {}}, "sampling": {}, "roots": {}})
            ))
        );
        // `elicitation: {}` is form only
        assert_eq!(
            undeclared(&json!({"a": elicit}), &json!({"elicitation": {}})),
            Ok(None)
        );
        assert_eq!(
            undeclared(&json!({"b": url}), &json!({"elicitation": {}})),
            Ok(Some(json!({"elicitation": {"url": {}}})))
        );
        assert_eq!(
            undeclared(&json!({"a": elicit}), &json!({"sampling": {}})),
            Ok(Some(json!({"elicitation": {}})))
        );
        assert_eq!(undeclared(&json!({}), &json!({})), Ok(None));
        for bad in [
            json!({"a": {"method": "tools/call"}}),
            json!({"a": 1}),
            json!([1]),
        ] {
            assert_eq!(
                undeclared(&bad, &json!({})).unwrap_err().code,
                -32603,
                "{bad}"
            );
        }
        let e = missing_capability(json!({"sampling": {}}));
        assert_eq!(
            (e.code, e.data),
            (
                -32021,
                Some(json!({"requiredCapabilities": {"sampling": {}}}))
            )
        );
        assert!(tasks_declared(&json!({"extensions": {TASKS_EXT: {}}})));
        assert!(!tasks_declared(&json!({"extensions": {}})) && !tasks_declared(&json!({})));
        assert_eq!(
            tasks_required().data,
            Some(json!({"requiredCapabilities": {"extensions": {TASKS_EXT: {}}}}))
        );
    }

    /// §23.4: a state is ours, fresh, and for exactly this caller and request — or it is refused.
    #[test]
    fn request_state_is_signed_and_bound() {
        let key = hmac::Key::new(hmac::HMAC_SHA256, b"k");
        let bind = StateBinding {
            caller: "c1",
            group: "g",
            zone: "z",
            method: "tools/call",
            name: "t",
            args: "abc",
        };
        let state = json!({"round": 1, "nested": ["x"]});
        let tok = sign_state(&key, &bind, &state, 1000);
        assert_eq!(tok.split('.').count(), 2);
        assert_eq!(verify_state(&key, &bind, &tok, 999), Ok(state.clone()));
        assert_eq!(verify_state(&key, &bind, &tok, 1000), Err(BadState)); // expired
        assert_eq!(
            verify_state(&key, &bind, &format!("{tok}-TAMPERED"), 1),
            Err(BadState)
        );
        assert_eq!(verify_state(&key, &bind, "garbage", 1), Err(BadState));
        assert_eq!(verify_state(&key, &bind, "a.b", 1), Err(BadState));
        assert_eq!(verify_state(&key, &bind, "", 1), Err(BadState));
        let other = hmac::Key::new(hmac::HMAC_SHA256, b"other");
        assert_eq!(verify_state(&other, &bind, &tok, 1), Err(BadState));
        // every bound field matters
        for (i, field) in ["caller", "group", "zone", "method", "name", "args"]
            .iter()
            .enumerate()
        {
            let mut v = ["c1", "g", "z", "tools/call", "t", "abc"];
            v[i] = "different";
            let b = StateBinding {
                caller: v[0],
                group: v[1],
                zone: v[2],
                method: v[3],
                name: v[4],
                args: v[5],
            };
            assert_eq!(verify_state(&key, &b, &tok, 1), Err(BadState), "{field}");
        }
        // a payload swapped under a valid-looking token fails the MAC
        let (_, mac) = tok.split_once('.').unwrap();
        let forged = format!(
            "{}.{mac}",
            B64URL.encode(bind.payload(&json!("evil"), 1000).to_string())
        );
        assert_eq!(verify_state(&key, &bind, &forged, 1), Err(BadState));
        // a null state round-trips too
        let tok = sign_state(&key, &bind, &Value::Null, 10);
        assert_eq!(verify_state(&key, &bind, &tok, 1), Ok(Value::Null));
    }
}
