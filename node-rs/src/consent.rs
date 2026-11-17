//! Telemetry consent (CONTRACTS §24.3). With `RAMEN_TELEMETRY_CONSENT=1`, `tools/call`, `prompts/get` and
//! `resources/read` are served only to a caller who accepted the notice:
//!
//! 1. a console token with `tel: true`, or the console's own call (worker admin key) — decided in `grpc::guard`,
//!    never asked here;
//! 2. a 2026-07-28 client that declared `elicitation` is asked through MRTR (§23.4): input request
//!    `ramen_telemetry_consent` plus a signed `requestState`; `accept: true` on the retry is recorded for the
//!    caller binding (§16.2) for 30 days — scope Redis, else item Redis, else this pod's memory;
//! 3. a modern client without `elicitation` gets `-32021`, a 2025-06-18 client `-32003`.
//!
//! The consent round's `requestState` is signed for `<method>#consent`, so it can never verify as a tool's state
//! (nor a tool's as ours); once consent is on record the consent answer and that state are taken off the request
//! before the tool sees it, and the tool's own input requests follow in later rounds as usual.
use crate::config::Config;
use crate::grpc::App;
use crate::modern;
use crate::sidecar::RpcErr;
use serde_json::{Value, json};

/// The input request / response key of the consent round.
pub const KEY: &str = "ramen_telemetry_consent";
/// How long a recorded consent lasts.
pub const TTL_SECS: u64 = 30 * 24 * 3600;
pub const REQUIRED: &str = "consent_required";
pub const DECLINED: &str = "consent_declined";

/// Lists, `server/discover` and `completion/complete` are never gated: MRTR is not allowed on them.
pub fn gated(method: &str) -> bool {
    matches!(method, "tools/call" | "prompts/get" | "resources/read")
}

/// Where a caller's consent is recorded: per group (the scope Redis is shared by the group's zones) and binding.
pub fn store_key(cfg: &Config, binding: &str) -> String {
    format!("ramen:consent:{}:{binding}", cfg.group)
}

pub enum Gate {
    /// Served. `granted`: the consent was recorded on this very call. `params`: the request without the consent
    /// round's answer and state, when it carried either.
    Pass {
        granted: bool,
        params: Option<Value>,
    },
    /// Answered here, with the access-log reason.
    Answer(Result<Value, RpcErr>, &'static str),
}

fn prompt(cfg: &Config) -> Value {
    let at = match cfg.telemetry_notice.as_str() {
        "" => "its telemetry collector",
        host => host,
    };
    json!({"method": "elicitation/create", "params": {"mode": "form",
        "message": format!("This Ramen group records telemetry about your calls (tool name, timing, outcome — never \
                            arguments or results) at {at} and asks your agent to send its telemetry there. Accept to continue."),
        "requestedSchema": {"type": "object", "properties": {"accept": {"type": "boolean"}}, "required": ["accept"]}}})
}

/// `{"action": "accept", "content": {"accept": true}}` and nothing else.
fn accepted(answer: &Value) -> bool {
    answer["action"] == "accept" && answer["content"]["accept"] == true
}

/// The request as the tool should see it: no consent answer, and no consent-round state.
fn strip(params: &Value, own_state: bool) -> Option<Value> {
    let answered = params
        .get("inputResponses")
        .is_some_and(|r| r.get(KEY).is_some());
    if !answered && !own_state {
        return None;
    }
    let mut p = params.clone();
    let o = p.as_object_mut()?;
    if own_state {
        o.remove("requestState");
    }
    if let Some(Value::Object(r)) = o.get_mut("inputResponses") {
        r.remove(KEY);
        if r.is_empty() {
            o.remove("inputResponses");
        }
    }
    Some(p)
}

/// The gate itself, for a caller the guard did not already exempt. `binding` is the §16.2 caller binding.
pub async fn gate(
    app: &App,
    cfg: &Config,
    binding: &str,
    is_modern: bool,
    caps: &Value,
    method: &str,
    params: &Value,
) -> Gate {
    let key = store_key(cfg, binding);
    // a store that cannot be read is "not on record": the caller is asked again rather than let through
    let on_record = app.consent.get(&key).await.ok().flatten().is_some();
    if !is_modern {
        // a 2025-06-18 request has no retry; a consent given earlier through a modern client still counts
        if on_record {
            return Gate::Pass {
                granted: false,
                params: None,
            };
        }
        let err = RpcErr::new(
            -32003,
            "forbidden: this group requires telemetry consent; use a client that speaks MCP 2026-07-28 or sign in through OAuth",
        );
        return Gate::Answer(Err(err.because(REQUIRED)), REQUIRED);
    }
    let name = crate::mcp::target_name(method, params).unwrap_or_default();
    let args = crate::grpc::args_digest(params.get("arguments"));
    let round = format!("{method}#consent");
    let bind = modern::StateBinding {
        caller: binding,
        group: &cfg.group,
        zone: &cfg.zone,
        method: &round,
        name: &name,
        args: &args,
    };
    let now = crate::session::now();
    let own_state = params
        .get("requestState")
        .and_then(Value::as_str)
        .is_some_and(|t| modern::verify_state(&app.mrtr_key, &bind, t, now).is_ok());
    if on_record {
        return Gate::Pass {
            granted: false,
            params: strip(params, own_state),
        };
    }
    match params.get("inputResponses").and_then(|r| r.get(KEY)) {
        None if caps.get("elicitation").is_some_and(Value::is_object) => {
            let state = modern::sign_state(
                &app.mrtr_key,
                &bind,
                &json!({"ramen": "telemetry_consent"}),
                now + cfg.mrtr_ttl_secs,
            );
            Gate::Answer(
                Ok(
                    json!({"resultType": "input_required", "inputRequests": {KEY: prompt(cfg)}, "requestState": state}),
                ),
                REQUIRED,
            )
        }
        None => Gate::Answer(
            Err(modern::missing_capability(json!({"elicitation": {}})).because(REQUIRED)),
            REQUIRED,
        ),
        // an answer counts only with the state this node signed for this caller and this very request
        Some(_) if !own_state => Gate::Answer(
            Err(RpcErr::new(-32602, "requestState integrity check failed")
                .because(modern::STATE_INVALID)),
            modern::STATE_INVALID,
        ),
        Some(answer) if accepted(answer) => {
            let record = json!({"at": crate::log::now_iso()});
            if let Err(e) = app.consent.put(&key, &record, TTL_SECS).await {
                // the caller did accept: serve the call; they are asked again next time
                crate::log::emit("warn", "consent store unavailable", json!({"error": e}));
            }
            Gate::Pass {
                granted: true,
                params: strip(params, true),
            }
        }
        Some(_) => Gate::Answer(
            Err(RpcErr::new(-32602, "telemetry consent declined").because(DECLINED)),
            DECLINED,
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_three_call_methods_are_gated() {
        for m in ["tools/call", "prompts/get", "resources/read"] {
            assert!(gated(m), "{m}");
        }
        for m in [
            "tools/list",
            "prompts/list",
            "resources/list",
            "resources/templates/list",
            "server/discover",
            "completion/complete",
            "initialize",
            "ping",
            "tasks/get",
            "subscriptions/listen",
        ] {
            assert!(!gated(m), "{m}");
        }
    }

    #[test]
    fn accept_means_the_action_and_the_checkbox() {
        assert!(accepted(
            &json!({"action": "accept", "content": {"accept": true}})
        ));
        for no in [
            json!({"action": "accept", "content": {"accept": false}}),
            json!({"action": "accept", "content": {"accept": "true"}}),
            json!({"action": "accept"}),
            json!({"action": "decline"}),
            json!({"action": "cancel"}),
            json!({"action": "decline", "content": {"accept": true}}),
            json!({"accept": true}),
            json!({}),
        ] {
            assert!(!accepted(&no), "{no}");
        }
    }

    #[test]
    fn the_tool_never_sees_the_consent_round() {
        let p = json!({"name": "ask", "arguments": {"a": 1}, "requestState": "ours",
                       "inputResponses": {KEY: {"action": "accept"}}});
        assert_eq!(
            strip(&p, true).unwrap(),
            json!({"name": "ask", "arguments": {"a": 1}})
        );
        // a later round: the client still sends the consent answer next to the tool's own, with the tool's state
        let p = json!({"name": "ask", "requestState": "the tool's",
                       "inputResponses": {KEY: {"action": "accept"}, "user_name": {"action": "accept"}}});
        assert_eq!(
            strip(&p, false).unwrap(),
            json!({"name": "ask", "requestState": "the tool's", "inputResponses": {"user_name": {"action": "accept"}}})
        );
        // nothing of ours on it: untouched (no copy)
        assert!(strip(&json!({"name": "ask", "requestState": "x"}), false).is_none());
    }

    #[test]
    fn the_prompt_is_a_form_with_one_required_checkbox_and_names_the_host() {
        let c = |notice: &str| {
            let m: std::collections::HashMap<_, _> =
                [("RAMEN_TELEMETRY_NOTICE".to_string(), notice.to_string())].into();
            Config::from_map(&m).unwrap()
        };
        let p = prompt(&c("jaeger.ramen-system:4318"));
        assert_eq!(
            (&p["method"], &p["params"]["mode"]),
            (&json!("elicitation/create"), &json!("form"))
        );
        assert_eq!(
            p["params"]["requestedSchema"]["properties"]["accept"]["type"],
            "boolean"
        );
        assert_eq!(
            p["params"]["requestedSchema"]["required"],
            json!(["accept"])
        );
        let msg = p["params"]["message"].as_str().unwrap();
        assert_eq!(
            msg,
            "This Ramen group records telemetry about your calls (tool name, timing, outcome — never arguments or \
             results) at jaeger.ramen-system:4318 and asks your agent to send its telemetry there. Accept to continue."
        );
        assert!(
            prompt(&c(""))["params"]["message"]
                .as_str()
                .unwrap()
                .contains("at its telemetry collector and")
        );
        // the modern client check of §23.4 accepts it for a client that declared elicitation
        let reqs = json!({ KEY: p });
        assert_eq!(
            modern::undeclared(&reqs, &json!({"elicitation": {}})).unwrap(),
            None
        );
    }
}
