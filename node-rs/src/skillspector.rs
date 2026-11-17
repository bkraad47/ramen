//! The SkillSpector policy gate (CONTRACTS §30.3). When the group requires it (`RAMEN_SKILLSPECTOR_REQUIRED=1`) a
//! gated call (`tools/call`, `prompts/get`, `resources/read`) from anyone but the console must name the policy
//! version it was signed in under: `params._meta["io.ramen/skillspector"] = {"version": "<v>"}`, or — a legacy body
//! through the bridge — top-level `params.skillspector`. Runs after the evals gate, before the runtime.
//!
//! - missing → `-32027 skillspector policy required` (HTTP 428, `data.version`); log reason `skillspector_required`
//! - `version` ≠ the worker's `skillspector_version` → `-32027 skillspector policy changed: sign in again`
//!   (HTTP 428, `data.version`); reason `skillspector_stale`
//! - the wrong shape → `-32602 invalid skillspector policy: <what>`, no reason
//!
//! A worker without a version (`RAMEN_SKILLSPECTOR_VERSION` empty) takes any. The tool never sees the field.
use crate::config::Config;
use crate::sidecar::RpcErr;
use serde_json::{Value, json};

/// The `_meta` key on the request.
pub const META: &str = "io.ramen/skillspector";
/// The top-level key a legacy body may carry instead.
pub const FIELD: &str = "skillspector";
pub const REQUIRED: &str = "skillspector_required";
pub const STALE: &str = "skillspector_stale";

fn with_version(cfg: &Config, e: RpcErr) -> RpcErr {
    e.with_data(json!({"version": cfg.skillspector_version}))
}

pub fn required(cfg: &Config) -> RpcErr {
    with_version(cfg, RpcErr::new(-32027, "skillspector policy required")).because(REQUIRED)
}

fn stale(cfg: &Config) -> RpcErr {
    with_version(
        cfg,
        RpcErr::new(-32027, "skillspector policy changed: sign in again"),
    )
    .because(STALE)
}

/// The version named on the call: `_meta` first, the legacy top-level field else.
fn named(params: &Value) -> Option<&Value> {
    params
        .pointer(&format!("/_meta/{}", META.replace('/', "~1")))
        .or_else(|| params.get(FIELD))
}

/// The version checked (`-32602` on the wrong shape, `-32027` when stale).
pub fn check(cfg: &Config, raw: &Value) -> Result<(), RpcErr> {
    let Some(version) = raw.get("version").and_then(Value::as_str) else {
        return Err(RpcErr::new(
            -32602,
            "invalid skillspector policy: version must be a string",
        ));
    };
    if !cfg.skillspector_version.is_empty() && version != cfg.skillspector_version {
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
            (
                "RAMEN_SKILLSPECTOR_VERSION".to_string(),
                version.to_string(),
            ),
            ("RAMEN_SKILLSPECTOR_REQUIRED".to_string(), "1".to_string()),
        ]
        .into();
        Config::from_map(&m).unwrap()
    }

    #[test]
    fn gate_takes_meta_or_the_legacy_field_and_strips_it() {
        let c = cfg("s1");
        let params = json!({"name": "echo", "arguments": {"q": 1},
                            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                      META: {"version": "s1"}}});
        assert_eq!(
            gate(&c, &params).unwrap(),
            json!({"name": "echo", "arguments": {"q": 1},
                   "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}})
        );
        let legacy = json!({"name": "echo", FIELD: {"version": "s1"}});
        assert_eq!(gate(&c, &legacy).unwrap(), json!({"name": "echo"}));
    }

    #[test]
    fn missing_stale_malformed_and_no_version_known() {
        let c = cfg("s1");
        let e = gate(&c, &json!({"name": "echo"})).unwrap_err();
        assert_eq!((e.code, e.reason), (-32027, Some(REQUIRED)));
        assert_eq!(e.message, "skillspector policy required");
        assert_eq!(e.data, Some(json!({"version": "s1"})));
        let e = gate(
            &c,
            &json!({"name": "echo", "_meta": {META: {"version": "s0"}}}),
        )
        .unwrap_err();
        assert_eq!((e.code, e.reason), (-32027, Some(STALE)));
        assert_eq!(e.message, "skillspector policy changed: sign in again");
        assert_eq!(e.data, Some(json!({"version": "s1"})));
        for bad in [json!({}), json!({"version": 1}), json!("s1"), json!(null)] {
            let e = gate(&c, &json!({"name": "echo", FIELD: bad})).unwrap_err();
            assert_eq!((e.code, e.reason), (-32602, None));
            assert!(e.message.starts_with("invalid skillspector policy: "));
        }
        // a worker never told a version takes any
        assert!(gate(&cfg(""), &json!({"_meta": {META: {"version": "zzz"}}})).is_ok());
        assert_eq!(gate(&cfg(""), &json!({})).unwrap_err().code, -32027);
    }
}
