//! The evals gate (CONTRACTS §29.3). When the group requires evals (`RAMEN_EVALS_REQUIRED=1`) a gated call
//! (`tools/call`, `prompts/get`, `resources/read`) from anyone but the console must name the evals config version
//! it was signed in under: `params._meta["io.ramen/evals"] = {"version": "<v>"}`, or — a legacy body through the
//! bridge — top-level `params.evals`. The same gate on both eras, after the telemetry gate, before the runtime.
//!
//! - missing → `-32026 evals config required` (HTTP 428, `data.version`); log reason `evals_required`
//! - `version` ≠ the worker's `evals_version` → `-32026 evals config changed: sign in again` (HTTP 428,
//!   `data.version`); reason `evals_stale`
//! - the wrong shape → `-32602 invalid evals config: <what>`, no reason
//!
//! A worker without a version (`RAMEN_EVALS_VERSION` empty) takes any. The tool never sees the field.
use crate::config::Config;
use crate::sidecar::RpcErr;
use serde_json::{Value, json};

/// The `_meta` key on the request.
pub const META: &str = "io.ramen/evals";
/// The top-level key a legacy body may carry instead.
pub const FIELD: &str = "evals";
pub const REQUIRED: &str = "evals_required";
pub const STALE: &str = "evals_stale";

fn with_version(cfg: &Config, e: RpcErr) -> RpcErr {
    e.with_data(json!({"version": cfg.evals_version}))
}

pub fn required(cfg: &Config) -> RpcErr {
    with_version(cfg, RpcErr::new(-32026, "evals config required")).because(REQUIRED)
}

fn stale(cfg: &Config) -> RpcErr {
    with_version(
        cfg,
        RpcErr::new(-32026, "evals config changed: sign in again"),
    )
    .because(STALE)
}

/// The version named on the call: `_meta` first, the legacy top-level field else.
fn named(params: &Value) -> Option<&Value> {
    params
        .pointer(&format!("/_meta/{}", META.replace('/', "~1")))
        .or_else(|| params.get(FIELD))
}

/// The version checked (`-32602` on the wrong shape, `-32026` when stale).
pub fn check(cfg: &Config, raw: &Value) -> Result<(), RpcErr> {
    let Some(version) = raw.get("version").and_then(Value::as_str) else {
        return Err(RpcErr::new(
            -32602,
            "invalid evals config: version must be a string",
        ));
    };
    if !cfg.evals_version.is_empty() && version != cfg.evals_version {
        return Err(stale(cfg));
    }
    Ok(())
}

/// The gate of a gated call: the params without the version (what the tool sees).
pub fn gate(cfg: &Config, params: &Value) -> Result<Value, RpcErr> {
    let Some(raw) = named(params) else {
        return Err(required(cfg));
    };
    check(cfg, raw)?;
    let mut rest = params.clone();
    if let Some(Value::Object(m)) = rest.get_mut("_meta") {
        m.remove(META);
    }
    if let Some(m) = rest.as_object_mut() {
        m.remove(FIELD);
    }
    Ok(rest)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn cfg(version: &str) -> Config {
        let m: HashMap<_, _> = [
            ("RAMEN_EVALS_VERSION".to_string(), version.to_string()),
            ("RAMEN_EVALS_REQUIRED".to_string(), "1".to_string()),
        ]
        .into();
        Config::from_map(&m).unwrap()
    }

    #[test]
    fn gate_takes_meta_or_the_legacy_field_and_strips_it() {
        let c = cfg("v1");
        let params = json!({"name": "echo", "arguments": {"q": 1},
                            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                      META: {"version": "v1"}}});
        assert_eq!(
            gate(&c, &params).unwrap(),
            json!({"name": "echo", "arguments": {"q": 1},
                   "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}})
        );
        let legacy = json!({"name": "echo", FIELD: {"version": "v1"}});
        assert_eq!(gate(&c, &legacy).unwrap(), json!({"name": "echo"}));
    }

    #[test]
    fn missing_stale_malformed_and_no_version_known() {
        let c = cfg("v1");
        let e = gate(&c, &json!({"name": "echo"})).unwrap_err();
        assert_eq!((e.code, e.reason), (-32026, Some(REQUIRED)));
        assert_eq!(e.message, "evals config required");
        assert_eq!(e.data, Some(json!({"version": "v1"})));
        let e = gate(
            &c,
            &json!({"name": "echo", "_meta": {META: {"version": "v0"}}}),
        )
        .unwrap_err();
        assert_eq!((e.code, e.reason), (-32026, Some(STALE)));
        assert_eq!(e.message, "evals config changed: sign in again");
        assert_eq!(e.data, Some(json!({"version": "v1"})));
        for bad in [json!({}), json!({"version": 1}), json!("v1"), json!(null)] {
            let e = gate(&c, &json!({"name": "echo", FIELD: bad})).unwrap_err();
            assert_eq!((e.code, e.reason), (-32602, None));
            assert!(e.message.starts_with("invalid evals config: "));
        }
        // a worker never told a version takes any
        assert!(gate(&cfg(""), &json!({"_meta": {META: {"version": "zzz"}}})).is_ok());
        assert_eq!(gate(&cfg(""), &json!({})).unwrap_err().code, -32026);
    }
}
