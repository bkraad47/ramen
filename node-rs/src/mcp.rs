//! MCP JSON-RPC methods (CONTRACTS §3) → sidecar `runtime.*` calls.
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
            sc.call("runtime.call_tool", json!({"name": name, "arguments": params.get("arguments").cloned().unwrap_or(json!({}))})).await?
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
            sc.call("runtime.get_prompt", json!({"name": name, "arguments": params.get("arguments").cloned().unwrap_or(json!({}))})).await?
        }
        _ => return Err(RpcErr::new(-32601, format!("method not found: {method}"))),
    };
    Ok(Some(r))
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
}
