//! MCP JSON-RPC methods (CONTRACTS §3) → sidecar `runtime.*` calls.
use crate::sidecar::{RpcErr, Sidecar};
use serde_json::{Value, json};

pub const PROTOCOL_VERSION: &str = "2025-06-18";

/// `Ok(None)` for notifications (no response body).
pub async fn dispatch(sc: &Sidecar, method: &str, params: &Value) -> Result<Option<Value>, RpcErr> {
    if method.starts_with("notifications/") {
        return Ok(None);
    }
    let r = match method {
        "initialize" => json!({"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                               "serverInfo": {"name": "ramen-node", "version": env!("CARGO_PKG_VERSION")}}),
        "ping" => json!({}),
        "tools/list" => json!({"tools": loaded(sc, "tools").await?}),
        "resources/list" => json!({"resources": loaded(sc, "resources").await?}),
        "prompts/list" => json!({"prompts": loaded(sc, "prompts").await?}),
        "tools/call" => sc.call("runtime.call_tool", json!({"name": str_param(params, "name")?, "arguments": params.get("arguments").cloned().unwrap_or(json!({}))})).await?,
        "resources/read" => sc.call("runtime.read_resource", json!({"uri": str_param(params, "uri")?})).await?,
        "prompts/get" => sc.call("runtime.get_prompt", json!({"name": str_param(params, "name")?, "arguments": params.get("arguments").cloned().unwrap_or(json!({}))})).await?,
        _ => return Err(RpcErr::new(-32601, format!("method not found: {method}"))),
    };
    Ok(Some(r))
}

async fn loaded(sc: &Sidecar, key: &str) -> Result<Value, RpcErr> {
    sc.loaded()
        .await
        .map(|l| l.result[key].clone())
        .ok_or_else(|| RpcErr::new(-32002, "runtime not loaded"))
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
