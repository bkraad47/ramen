//! Telemetry through Ramen (CONTRACTS §26.3). When the group requires telemetry (`RAMEN_TELEMETRY_CONSENT=1`) a
//! gated call (`tools/call`, `prompts/get`, `resources/read`) from anyone but the console must carry
//! `params._meta["io.ramen/telemetry"]`: `{version, seq, events: [{ts, kind, name, ms, ok, reasoning?}]}` — what the
//! client did since its last report. The same object is accepted on `telemetry/report` between calls.
//!
//! - missing → `-32024` (HTTP 428, `data.version`); missing on a 2025-06-18 body → `-32003` (a legacy body that
//!   carries the report is served like a modern one: the bridge passes it through); log reason `telemetry_required`
//! - `version` ≠ the worker's `telemetry_version` → `-32025` (HTTP 428, `data.version`); reason `telemetry_stale`
//! - more than 200 events, more than 64 KiB, or the wrong shape → `-32602`
//!
//! Each event becomes a `CLIENT` span under the call's span (otel.rs) and a count; the events never reach the
//! access log (the verbose line is scrubbed) and the tool never sees the report. A worker without a version
//! (deployed before the console knew one) skips the version check rather than refusing everyone.
use crate::config::Config;
use crate::otel::SpanCtx;
use crate::sidecar::RpcErr;
use serde_json::{Value, json};
use std::net::IpAddr;
use std::sync::OnceLock;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// The `_meta` key, on the request and on `server/discover`'s answer.
pub const META: &str = "io.ramen/telemetry";
pub const MAX_EVENTS: usize = 200;
pub const MAX_BYTES: usize = 64 * 1024;
pub const MAX_REASONING: usize = 4096;
pub const REQUIRED: &str = "telemetry_required";
pub const STALE: &str = "telemetry_stale";
const LEGACY: &str = "forbidden: this group requires telemetry; use a client that reports it (MCP 2026-07-28 or \
                      the Ramen bridge)";

#[derive(Debug)]
pub struct Event {
    pub start: SystemTime,
    pub kind: String,
    pub name: String,
    pub ms: u64,
    pub ok: bool,
    /// Already cut to `MAX_REASONING` bytes.
    pub reasoning: Option<String>,
}

#[derive(Debug)]
pub struct Report {
    pub seq: u64,
    pub events: Vec<Event>,
}

/// Who reported, for the spans' attributes.
pub struct Sender<'a> {
    pub ip: IpAddr,
    /// The token's email, else its subject, else the key id.
    pub user: &'a str,
    pub key_id: &'a str,
    pub transport: &'a str,
    /// The call's span; `None` while export is off, and then the events have nowhere to go.
    pub span: Option<&'a SpanCtx>,
}

/// `ramen.worker`: the pod's hostname.
pub fn worker() -> &'static str {
    static NAME: OnceLock<String> = OnceLock::new();
    NAME.get_or_init(|| {
        std::env::var("HOSTNAME")
            .ok()
            .or_else(|| std::fs::read_to_string("/etc/hostname").ok())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "unknown".into())
    })
}

fn invalid(what: &str) -> RpcErr {
    RpcErr::new(-32602, format!("invalid telemetry report: {what}"))
}

fn with_version(cfg: &Config, e: RpcErr) -> RpcErr {
    e.with_data(json!({"version": cfg.telemetry_version}))
}

/// `-32024` on the modern era, `-32003` with the pinned text on the legacy one.
pub fn required(cfg: &Config, modern: bool) -> RpcErr {
    let e = if modern {
        with_version(cfg, RpcErr::new(-32024, "telemetry report required"))
    } else {
        RpcErr::new(-32003, LEGACY)
    };
    RpcErr {
        reason: Some(REQUIRED),
        ..e
    }
}

fn stale(cfg: &Config) -> RpcErr {
    RpcErr {
        reason: Some(STALE),
        ..with_version(
            cfg,
            RpcErr::new(-32025, "telemetry config changed: sign in again"),
        )
    }
}

/// The report checked: shape and limits (`-32602`), then its version against the worker's (`-32025`).
pub fn parse(cfg: &Config, report: &Value) -> Result<Report, RpcErr> {
    let Some(o) = report.as_object() else {
        return Err(invalid("not an object"));
    };
    let Some(version) = o.get("version").and_then(Value::as_str) else {
        return Err(invalid("version"));
    };
    let Some(seq) = o.get("seq").and_then(Value::as_u64) else {
        return Err(invalid("seq"));
    };
    let Some(events) = o.get("events").and_then(Value::as_array) else {
        return Err(invalid("events"));
    };
    if events.len() > MAX_EVENTS {
        return Err(invalid("more than 200 events"));
    }
    if report.to_string().len() > MAX_BYTES {
        return Err(invalid("more than 64 KiB"));
    }
    let mut out = Vec::with_capacity(events.len());
    for (i, e) in events.iter().enumerate() {
        let field = |k: &str| e.get(k).filter(|v| !v.is_null());
        let bad = |k: &str| invalid(&format!("events[{i}].{k}"));
        let start = field("ts")
            .and_then(Value::as_str)
            .and_then(rfc3339)
            .ok_or_else(|| bad("ts"))?;
        let kind = match field("kind").and_then(Value::as_str) {
            Some(k @ ("mcp" | "llm" | "tool" | "other")) => k.to_string(),
            _ => return Err(bad("kind")),
        };
        let name = field("name")
            .and_then(Value::as_str)
            .ok_or_else(|| bad("name"))?
            .to_string();
        let ms = field("ms")
            .and_then(Value::as_u64)
            .ok_or_else(|| bad("ms"))?;
        let ok = field("ok")
            .and_then(Value::as_bool)
            .ok_or_else(|| bad("ok"))?;
        let reasoning = match field("reasoning") {
            None => None,
            Some(Value::String(s)) => Some(cut(s, MAX_REASONING)),
            Some(_) => return Err(bad("reasoning")),
        };
        out.push(Event {
            start,
            kind,
            name,
            ms,
            ok,
            reasoning,
        });
    }
    if !cfg.telemetry_version.is_empty() && version != cfg.telemetry_version {
        return Err(stale(cfg));
    }
    Ok(Report { seq, events: out })
}

/// The first `max` bytes, on a character boundary.
fn cut(s: &str, max: usize) -> String {
    let mut end = max.min(s.len());
    while !s.is_char_boundary(end) {
        end -= 1;
    }
    s[..end].to_string()
}

/// `YYYY-MM-DDTHH:MM:SS[.frac](Z|±HH:MM)` → the instant.
pub fn rfc3339(s: &str) -> Option<SystemTime> {
    let b = s.as_bytes();
    if b.len() < 20 || !b[10].eq_ignore_ascii_case(&b'T') || b[4] != b'-' || b[7] != b'-' {
        return None;
    }
    let num = |r: std::ops::Range<usize>| s.get(r)?.parse::<i64>().ok();
    let (y, m, d) = (num(0..4)?, num(5..7)?, num(8..10)?);
    let (hh, mm, ss) = (num(11..13)?, num(14..16)?, num(17..19)?);
    if !(1..=12).contains(&m) || !(1..=31).contains(&d) || hh > 23 || mm > 59 || ss > 60 {
        return None;
    }
    let mut i = 19;
    let mut nanos: u32 = 0;
    if b[i] == b'.' {
        i += 1;
        let from = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        let frac = s.get(from..i)?;
        if frac.is_empty() {
            return None;
        }
        nanos = format!("{frac:0<9}")[..9].parse().ok()?;
    }
    let offset = match b.get(i)? {
        b'Z' | b'z' if i + 1 == b.len() => 0,
        b'+' | b'-' if i + 6 == b.len() && b[i + 3] == b':' => {
            let (oh, om) = (num(i + 1..i + 3)?, num(i + 4..i + 6)?);
            let secs = oh * 3600 + om * 60;
            if b[i] == b'+' { secs } else { -secs }
        }
        _ => return None,
    };
    let days = days_from_civil(y, m, d);
    let secs = days * 86400 + hh * 3600 + mm * 60 + ss - offset;
    let t = UNIX_EPOCH.checked_add(Duration::new(u64::try_from(secs).ok()?, nanos))?;
    Some(t)
}

fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

/// The gate of a gated call: the report, and the params without it (what the tool sees).
pub fn gate(cfg: &Config, modern: bool, params: &Value) -> Result<(Report, Value), RpcErr> {
    let Some(raw) = params.pointer(&format!("/_meta/{}", META.replace('/', "~1"))) else {
        return Err(required(cfg, modern));
    };
    let report = parse(cfg, raw)?;
    let mut rest = params.clone();
    if let Some(Value::Object(m)) = rest.get_mut("_meta") {
        m.remove(META);
    }
    Ok((report, rest))
}

/// Each event as a `CLIENT` span under the call's span, and a count by kind / ok. Nothing happens without a span.
pub fn forward(cfg: &Config, who: &Sender<'_>, report: &Report) {
    let Some(span) = who.span else {
        return;
    };
    for e in &report.events {
        let mut attrs = vec![
            ("ramen.client.ip", who.ip.to_string()),
            ("ramen.user", who.user.to_string()),
            ("ramen.key_id", who.key_id.to_string()),
            ("ramen.group", cfg.group.clone()),
            ("ramen.zone", cfg.zone.clone()),
            ("ramen.env", cfg.env.clone()),
            ("ramen.worker", worker().to_string()),
            ("ramen.event.kind", e.kind.clone()),
            ("ramen.event.seq", report.seq.to_string()),
            ("ramen.transport", who.transport.to_string()),
        ];
        if let Some(r) = &e.reasoning {
            attrs.push(("ramen.reasoning", r.clone()));
        }
        span.client_event(
            format!("client {} {}", e.kind, e.name),
            e.start,
            Duration::from_millis(e.ms),
            e.ok,
            &e.kind,
            attrs,
        );
    }
}

/// `telemetry/report` (modern, authenticated, never consent-gated): `{"accepted": n, "version": "<current>"}`.
pub fn report(cfg: &Config, who: Option<&Sender<'_>>, params: &Value) -> Result<Value, RpcErr> {
    let r = parse(cfg, params)?;
    if let Some(who) = who {
        forward(cfg, who, &r);
    }
    Ok(json!({"accepted": r.events.len(), "version": cfg.telemetry_version}))
}

/// The request as the verbose access log may quote it: without the report.
pub fn scrub(mut rpc: Value) -> Value {
    if let Some(Value::Object(m)) = rpc.pointer_mut("/params/_meta") {
        m.remove(META);
    }
    if rpc["method"] == "telemetry/report"
        && let Some(Value::Object(p)) = rpc.get_mut("params")
    {
        p.remove("events");
    }
    rpc
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn cfg(version: &str) -> Config {
        let m: HashMap<_, _> =
            [("RAMEN_TELEMETRY_VERSION".to_string(), version.to_string())].into();
        Config::from_map(&m).unwrap()
    }

    fn event(extra: Value) -> Value {
        let mut e =
            json!({"ts": "2026-11-14T10:00:00Z", "kind": "llm", "name": "m", "ms": 10, "ok": true});
        e.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        e
    }

    #[test]
    fn rfc3339_shapes() {
        let secs =
            |s: &str| rfc3339(s).map(|t| t.duration_since(UNIX_EPOCH).unwrap().as_secs_f64());
        assert_eq!(secs("2026-11-14T10:00:00Z"), Some(1794650400.0));
        assert_eq!(secs("1970-01-01T00:00:00Z"), Some(0.0));
        assert_eq!(secs("2026-11-14T10:00:01.250+01:00"), Some(1794646801.25));
        assert_eq!(secs("2026-11-14T10:00:00.5-00:30"), Some(1794652200.5));
        assert_eq!(secs("2026-11-14t10:00:00z"), Some(1794650400.0));
        for bad in [
            "",
            "2026-11-14",
            "2026-11-14T10:00:00",
            "2026-11-14T10:00:00.Z",
            "2026-11-14T10:00:00+0100",
            "2026-13-14T10:00:00Z",
            "2026-11-14T25:00:00Z",
            "1969-12-31T23:59:59Z",
            "20261114T100000Z",
        ] {
            assert!(rfc3339(bad).is_none(), "{bad}");
        }
    }

    #[test]
    fn parse_checks_shape_limits_then_version() {
        let c = cfg("v1");
        let ok = json!({"version": "v1", "seq": 2, "events": [event(json!({"reasoning": "why"})),
                        event(json!({"kind": "tool", "ok": false, "ms": 0}))]});
        let r = parse(&c, &ok).unwrap();
        assert_eq!((r.seq, r.events.len()), (2, 2));
        assert_eq!(r.events[0].reasoning.as_deref(), Some("why"));
        assert!(r.events[1].reasoning.is_none() && !r.events[1].ok);
        // empty events is a report; reasoning is cut to 4 KiB on a char boundary
        let r = parse(&c, &json!({"version": "v1", "seq": 0, "events": []})).unwrap();
        assert!(r.events.is_empty());
        let long = format!("{}é", "a".repeat(4095));
        let r = parse(
            &c,
            &json!({"version": "v1", "seq": 1, "events": [event(json!({"reasoning": long}))]}),
        )
        .unwrap();
        assert_eq!(r.events[0].reasoning.as_ref().unwrap().len(), 4095);
        // the wrong shape and the limits are -32602 without a reason
        let many: Vec<Value> = (0..201).map(|_| event(json!({}))).collect();
        for bad in [
            json!([]),
            json!({"seq": 1, "events": []}),
            json!({"version": "v1", "events": []}),
            json!({"version": "v1", "seq": -1, "events": []}),
            json!({"version": "v1", "seq": 1}),
            json!({"version": "v1", "seq": 1, "events": many}),
            json!({"version": "v1", "seq": 1, "events": [event(json!({"reasoning": "r".repeat(70_000)}))]}),
            json!({"version": "v1", "seq": 1, "events": [event(json!({"kind": "model"}))]}),
            json!({"version": "v1", "seq": 1, "events": [event(json!({"ts": "yesterday"}))]}),
            json!({"version": "v1", "seq": 1, "events": [event(json!({"ms": -1}))]}),
            json!({"version": "v1", "seq": 1, "events": [event(json!({"ok": "yes"}))]}),
            json!({"version": "v1", "seq": 1, "events": [event(json!({"reasoning": 1}))]}),
            json!({"version": "v1", "seq": 1, "events": [{"kind": "llm"}]}),
        ] {
            let e = parse(&c, &bad).unwrap_err();
            assert_eq!((e.code, e.reason), (-32602, None), "{bad}: {}", e.message);
            assert!(e.message.starts_with("invalid telemetry report: "));
        }
        // a stale version, after the shape check: -32025 naming the current one
        let e = parse(&c, &json!({"version": "v0", "seq": 1, "events": []})).unwrap_err();
        assert_eq!((e.code, e.reason), (-32025, Some(STALE)));
        assert_eq!(e.data, Some(json!({"version": "v1"})));
        assert_eq!(e.message, "telemetry config changed: sign in again");
        // a worker that was never told a version takes any
        assert!(parse(&cfg(""), &json!({"version": "v0", "seq": 1, "events": []})).is_ok());
    }

    #[test]
    fn gate_strips_the_report_and_names_what_is_missing() {
        let c = cfg("v1");
        let params = json!({"name": "echo", "arguments": {"q": 1},
                            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                      META: {"version": "v1", "seq": 9, "events": [event(json!({}))]}}});
        let (r, rest) = gate(&c, true, &params).unwrap();
        assert_eq!(r.seq, 9);
        assert_eq!(
            rest,
            json!({"name": "echo", "arguments": {"q": 1},
                   "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}})
        );
        let e = gate(&c, true, &json!({"name": "echo"})).unwrap_err();
        assert_eq!((e.code, e.reason), (-32024, Some(REQUIRED)));
        assert_eq!(e.message, "telemetry report required");
        assert_eq!(e.data, Some(json!({"version": "v1"})));
        let e = gate(&c, false, &json!({"name": "echo"})).unwrap_err();
        assert_eq!((e.code, e.reason, e.data), (-32003, Some(REQUIRED), None));
        assert_eq!(e.message, LEGACY);
    }

    #[test]
    fn report_answers_and_the_log_never_sees_events() {
        let c = cfg("v1");
        let r = report(
            &c,
            None,
            &json!({"version": "v1", "seq": 1, "events": [event(json!({}))]}),
        )
        .unwrap();
        assert_eq!(r, json!({"accepted": 1, "version": "v1"}));
        assert_eq!(
            report(&c, None, &json!({"version": "v2", "seq": 1, "events": []}))
                .unwrap_err()
                .code,
            -32025
        );
        let rpc = json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "echo", "_meta": {"x": 1, META: {"events": [{"reasoning": "secret-thought"}]}}}});
        let s = scrub(rpc).to_string();
        assert!(
            !s.contains("secret-thought") && s.contains(r#""x":1"#),
            "{s}"
        );
        let rpc = json!({"method": "telemetry/report", "params": {"version": "v1", "seq": 1,
                         "events": [{"reasoning": "secret-thought"}]}});
        let s = scrub(rpc).to_string();
        assert!(!s.contains("secret-thought") && s.contains("seq"), "{s}");
        assert!(!worker().is_empty());
    }
}
