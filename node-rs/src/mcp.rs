//! MCP JSON-RPC methods (CONTRACTS §3 methods, carried over gRPC per §11) → sidecar `runtime.*` calls.
use crate::sidecar::{RpcErr, Sidecar};
use serde_json::{Value, json};

pub const PROTOCOL_VERSION: &str = "2025-06-18";

/// `Ok(None)` for notifications (no response body). `blocked` (CONTRACTS §9, `RAMEN_BLOCKED`) hides names /
/// resource URIs from the list calls and answers `-32601` for calls to them.
pub async fn dispatch(
    sc: &Sidecar,
    blocked: &[String],
    method: &str,
    params: &Value,
) -> Result<Option<Value>, RpcErr> {
    if method.starts_with("notifications/") {
        return Ok(None);
    }
    let r = match method {
        "initialize" => {
            json!({"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                               "serverInfo": {"name": "ramen-node", "version": env!("CARGO_PKG_VERSION")}})
        }
        "ping" => json!({}),
        "tools/list" => json!({"tools": loaded(sc, "tools", blocked).await?}),
        "resources/list" => json!({"resources": loaded(sc, "resources", blocked).await?}),
        "prompts/list" => json!({"prompts": loaded(sc, "prompts", blocked).await?}),
        "tools/call" => {
            let name = str_param(params, "name")?;
            deny_if_blocked(blocked, name, "tool")?;
            let arguments = params.get("arguments").cloned().unwrap_or(json!({}));
            // N8: the sidecar validates the same schema (executor.py `jsonschema.validate`); catching it here
            // too saves the round trip and matches the exact result shape a sidecar validation failure uses
            // (`isError: true`, not a JSON-RPC error — argument problems are a tool-call outcome, not a
            // protocol fault, CONTRACTS §2).
            if let Some(msg) = invalid_tool_args(sc, name, &arguments).await {
                return Ok(Some(
                    json!({"content": [{"type": "text", "text": msg}], "isError": true}),
                ));
            }
            sc.call(
                "runtime.call_tool",
                json!({"name": name, "arguments": arguments}),
            )
            .await?
        }
        "resources/read" => {
            let uri = str_param(params, "uri")?;
            deny_if_blocked(blocked, uri, "resource")?;
            if let Some(name) = resource_name(sc, uri).await {
                deny_if_blocked(blocked, &name, "resource")?;
            }
            sc.call("runtime.read_resource", json!({"uri": uri}))
                .await?
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
            sc.call(
                "runtime.get_prompt",
                json!({"name": name, "arguments": arguments}),
            )
            .await?
        }
        _ => return Err(RpcErr::new(-32601, format!("method not found: {method}"))),
    };
    Ok(Some(r))
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
    ["name", "uri"]
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

/// `name` of the loaded resource with this URI (so a resource blocked by name is also unreadable by URI).
async fn resource_name(sc: &Sidecar, uri: &str) -> Option<String> {
    let l = sc.loaded().await?;
    l.result["resources"]
        .as_array()?
        .iter()
        .find(|r| r.get("uri").and_then(Value::as_str) == Some(uri))
        .and_then(|r| r.get("name").and_then(Value::as_str).map(String::from))
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
