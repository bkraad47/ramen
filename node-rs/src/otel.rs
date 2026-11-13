//! OpenTelemetry export (CONTRACTS §24.3): one `SERVER` span per MCP call, a child span around the sidecar round
//! trip, and the counters `ramen.mcp.calls` / `ramen.mcp.errors` — OTLP/HTTP JSON to `{endpoint}/v1/traces` and
//! `/v1/metrics`.
//!
//! Nothing here runs on the call path: a finished span is one `try_send` into a bounded queue (full = dropped), and
//! a background task batches and posts. An unreachable collector loses data and logs one warning a minute; it never
//! fails or slows a call. What is exported is a fixed list of attributes (method, name, protocol, group/zone/env,
//! transport, status, reason, key id, guardrail stage/engine) — never arguments, results, credentials or secrets.
use crate::config::Config;
use crate::log::emit;
use crate::session::random;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::{mpsc, oneshot};

/// Finished spans and counts waiting for the exporter; beyond it they are dropped.
const QUEUE: usize = 4096;
/// A batch is posted when it reaches this many spans, or every `FLUSH`.
const BATCH: usize = 512;
const FLUSH: Duration = Duration::from_secs(2);
const POST_TIMEOUT: Duration = Duration::from_secs(5);
/// Distinct (method, name, status) series kept; a caller can name tools that do not exist, so past this the name
/// is folded into `(other)`.
const MAX_SERIES: usize = 2000;
const WARN_EVERY: Duration = Duration::from_secs(60);

const KIND_INTERNAL: u8 = 1;
const KIND_SERVER: u8 = 2;

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn nanos(t: SystemTime) -> String {
    t.duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0)
        .to_string()
}

fn attrs(pairs: &[(&str, String)]) -> Value {
    pairs
        .iter()
        .map(|(k, v)| json!({"key": k, "value": {"stringValue": v}}))
        .collect()
}

struct Span {
    trace_id: String,
    span_id: String,
    parent: Option<String>,
    state: Option<String>,
    name: String,
    kind: u8,
    start: SystemTime,
    end: SystemTime,
    attrs: Vec<(&'static str, String)>,
    error: bool,
}

impl Span {
    fn otlp(&self) -> Value {
        let mut s = json!({"traceId": self.trace_id, "spanId": self.span_id, "name": self.name, "kind": self.kind,
                           "startTimeUnixNano": nanos(self.start), "endTimeUnixNano": nanos(self.end),
                           "attributes": attrs(&self.attrs)});
        if let Some(p) = &self.parent {
            s["parentSpanId"] = json!(p);
        }
        if let Some(t) = &self.state {
            s["traceState"] = json!(t);
        }
        if self.error {
            s["status"] = json!({"code": 2}); // STATUS_CODE_ERROR
        }
        s
    }
}

enum Item {
    Span(Box<Span>),
    Count {
        method: String,
        name: Option<String>,
        status: String,
    },
    Flush(oneshot::Sender<()>),
}

/// Where the batches go; replaced on `Admin/Reload`.
struct Target {
    endpoint: String,
    headers: Vec<(String, String)>,
    resource: Value,
}

#[derive(Default)]
struct Inner {
    target: RwLock<Option<Arc<Target>>>,
    on: AtomicBool,
    dropped: AtomicU64,
}

pub struct Otel {
    inner: Arc<Inner>,
    tx: mpsc::Sender<Item>,
}

impl Otel {
    /// Starts the exporter task (idle while no endpoint is configured); it ends when the `Otel` is dropped.
    pub fn new(cfg: &Config) -> Arc<Self> {
        let inner = Arc::new(Inner::default());
        let (tx, rx) = mpsc::channel(QUEUE);
        tokio::spawn(export(inner.clone(), rx));
        let me = Arc::new(Otel { inner, tx });
        me.configure(cfg);
        me
    }

    /// `RAMEN_OTEL_ENDPOINT` / `RAMEN_OTEL_HEADERS` as they are now (startup and every `Admin/Reload`).
    pub fn configure(&self, cfg: &Config) {
        let target = cfg.otel_endpoint.as_ref().map(|endpoint| {
            Arc::new(Target {
                endpoint: endpoint.clone(),
                headers: cfg.otel_headers.clone(),
                resource: json!({"attributes": attrs(&[
                    ("service.name", "ramen-node".into()),
                    ("service.version", env!("CARGO_PKG_VERSION").into()),
                    ("ramen.group", cfg.group.clone()),
                    ("ramen.zone", cfg.zone.clone()),
                ])}),
            })
        });
        self.inner.on.store(target.is_some(), Relaxed);
        *self.inner.target.write().unwrap_or_else(|e| e.into_inner()) = target;
    }

    pub fn enabled(&self) -> bool {
        self.inner.on.load(Relaxed)
    }

    /// Spans and counts dropped because the queue was full.
    pub fn dropped(&self) -> u64 {
        self.inner.dropped.load(Relaxed)
    }

    /// The span of a call that starts now; `None` while export is off, so a node without a collector does no work.
    pub fn begin(self: &Arc<Self>) -> Option<SpanCtx> {
        self.enabled().then(|| SpanCtx {
            otel: self.clone(),
            trace_id: hex(&random(16)),
            span_id: hex(&random(8)),
            parent: None,
            state: None,
            start: SystemTime::now(),
            attrs: Vec::new(),
        })
    }

    fn send(&self, item: Item) {
        if self.tx.try_send(item).is_err() {
            self.inner.dropped.fetch_add(1, Relaxed);
        }
    }

    /// Posts what is queued and waits for it (shutdown, tests); gives up after `POST_TIMEOUT` twice over.
    pub async fn flush(&self) {
        let (done, wait) = oneshot::channel();
        if self.tx.send(Item::Flush(done)).await.is_ok() {
            let _ = tokio::time::timeout(POST_TIMEOUT * 2 + Duration::from_secs(1), wait).await;
        }
    }
}

/// One call's `SERVER` span while the call runs.
pub struct SpanCtx {
    otel: Arc<Otel>,
    trace_id: String,
    span_id: String,
    parent: Option<String>,
    state: Option<String>,
    start: SystemTime,
    /// Attributes the dispatch adds beyond the access-log fields (`ramen.guardrail.*`).
    pub attrs: Vec<(&'static str, String)>,
}

impl SpanCtx {
    /// §23.2: continue the caller's trace — `traceparent` is `<version>-<trace id>-<parent span id>-<flags>`,
    /// already checked for its shape by `modern::trace`.
    pub fn adopt(&mut self, trace: &Value) {
        let Some(tp) = trace["traceparent"].as_str() else {
            return;
        };
        let parts: Vec<&str> = tp.split('-').collect();
        if let [_, trace_id, parent, _] = parts[..]
            && trace_id.bytes().any(|b| b != b'0')
            && parent.bytes().any(|b| b != b'0')
        {
            self.trace_id = trace_id.to_string();
            self.parent = Some(parent.to_string());
            self.state = trace["tracestate"].as_str().map(String::from);
        }
    }

    /// A finished child span (the sidecar round trip) that began at `start`.
    pub fn child(&self, name: String, start: SystemTime, ok: bool) {
        self.otel.send(Item::Span(Box::new(Span {
            trace_id: self.trace_id.clone(),
            span_id: hex(&random(8)),
            parent: Some(self.span_id.clone()),
            state: self.state.clone(),
            name,
            kind: KIND_INTERNAL,
            start,
            end: SystemTime::now(),
            attrs: Vec::new(),
            error: !ok,
        })));
    }

    /// The call ended: its span, and one count. `fields` are the access-log fields of the line being written.
    pub fn end(
        &self,
        method: &str,
        name: Option<&str>,
        status: &str,
        mut fields: Vec<(&'static str, String)>,
    ) {
        fields.extend(self.attrs.iter().cloned());
        self.otel.send(Item::Span(Box::new(Span {
            trace_id: self.trace_id.clone(),
            span_id: self.span_id.clone(),
            parent: self.parent.clone(),
            state: self.state.clone(),
            name: format!("mcp {method}"),
            kind: KIND_SERVER,
            start: self.start,
            end: SystemTime::now(),
            attrs: fields,
            error: status == "error",
        })));
        self.otel.send(Item::Count {
            method: method.to_string(),
            name: name.map(String::from),
            status: status.to_string(),
        });
    }
}

type Series = (String, Option<String>, String);

/// Cumulative counts since the process started, by (method, name, status).
struct Counters {
    since: SystemTime,
    calls: BTreeMap<Series, u64>,
    dirty: bool,
}

impl Counters {
    fn add(&mut self, method: String, name: Option<String>, status: String) {
        let mut key = (method, name, status);
        if !self.calls.contains_key(&key) && self.calls.len() >= MAX_SERIES {
            key.1 = Some("(other)".into());
        }
        *self.calls.entry(key).or_insert(0) += 1;
        self.dirty = true;
    }

    fn otlp(&self, resource: &Value) -> Value {
        let now = nanos(SystemTime::now());
        let points = |errors_only: bool| -> Value {
            self.calls
                .iter()
                .filter(|((_, _, status), _)| !errors_only || status != "ok")
                .map(|((method, name, status), n)| {
                    let mut a = vec![("mcp.method", method.clone())];
                    if let Some(name) = name {
                        a.push(("mcp.name", name.clone()));
                    }
                    a.push(("ramen.status", status.clone()));
                    json!({"attributes": attrs(&a), "startTimeUnixNano": nanos(self.since), "timeUnixNano": now,
                           "asInt": n.to_string()})
                })
                .collect()
        };
        let sum = |name: &str, errors_only: bool| {
            json!({"name": name, "unit": "{call}",
                   "sum": {"aggregationTemporality": 2, "isMonotonic": true, "dataPoints": points(errors_only)}})
        };
        json!({"resourceMetrics": [{"resource": resource, "scopeMetrics": [{"scope": scope(),
               "metrics": [sum("ramen.mcp.calls", false), sum("ramen.mcp.errors", true)]}]}]})
    }
}

fn scope() -> Value {
    json!({"name": "ramen-node", "version": env!("CARGO_PKG_VERSION")})
}

fn traces(resource: &Value, spans: &[Span]) -> Value {
    json!({"resourceSpans": [{"resource": resource, "scopeSpans": [{"scope": scope(),
           "spans": spans.iter().map(Span::otlp).collect::<Vec<_>>()}]}]})
}

async fn export(inner: Arc<Inner>, mut rx: mpsc::Receiver<Item>) {
    let mut spans: Vec<Span> = Vec::new();
    let mut counters = Counters {
        since: SystemTime::now(),
        calls: BTreeMap::new(),
        dirty: false,
    };
    let mut warned: Option<Instant> = None;
    let mut tick = tokio::time::interval(FLUSH);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        let done = tokio::select! {
            item = rx.recv() => match item {
                None => return, // the node is gone
                Some(Item::Span(s)) => {
                    spans.push(*s);
                    if spans.len() < BATCH {
                        continue;
                    }
                    None
                }
                Some(Item::Count { method, name, status }) => {
                    counters.add(method, name, status);
                    continue;
                }
                Some(Item::Flush(done)) => Some(done),
            },
            _ = tick.tick() => None,
        };
        let target = inner
            .target
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .clone();
        let mut failed = None;
        if let Some(t) = &target {
            if !spans.is_empty() {
                failed = post(t, "/v1/traces", traces(&t.resource, &spans))
                    .await
                    .err();
            }
            if counters.dirty {
                failed = post(t, "/v1/metrics", counters.otlp(&t.resource))
                    .await
                    .err()
                    .or(failed);
            }
        }
        // sent or not, the batch is done with: an unreachable collector loses data, it does not grow a backlog
        spans.clear();
        counters.dirty = false;
        if let Some(error) = failed
            && warned.is_none_or(|at| at.elapsed() >= WARN_EVERY)
        {
            warned = Some(Instant::now());
            emit(
                "warn",
                "telemetry export failed; data dropped",
                json!({"error": error, "dropped_on_overflow": inner.dropped.load(Relaxed)}),
            );
        }
        if let Some(done) = done {
            let _ = done.send(());
        }
    }
}

async fn post(t: &Target, path: &str, body: Value) -> Result<(), String> {
    let url = format!("{}{path}", t.endpoint);
    let (status, _) = crate::netc::post_json(
        &url,
        &t.headers,
        body.to_string().into_bytes(),
        POST_TIMEOUT,
    )
    .await?;
    if (200..300).contains(&status) {
        Ok(())
    } else {
        Err(format!("collector answered {status} on {path}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::netc::tests::recorder;
    use std::collections::HashMap;

    fn cfg(pairs: &[(&str, &str)]) -> Config {
        Config::from_map(
            &pairs
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect::<HashMap<_, _>>(),
        )
        .unwrap()
    }

    fn attr<'a>(span: &'a Value, key: &str) -> Option<&'a str> {
        span["attributes"]
            .as_array()?
            .iter()
            .find(|a| a["key"] == key)?["value"]["stringValue"]
            .as_str()
    }

    #[tokio::test]
    async fn off_without_an_endpoint() {
        let o = Otel::new(&cfg(&[]));
        assert!(!o.enabled() && o.begin().is_none());
        o.flush().await; // nothing to post, nothing to wait for
    }

    #[tokio::test]
    async fn spans_and_counts_reach_the_collector_as_otlp_json() {
        let (addr, seen) = recorder(200).await;
        let o = Otel::new(&cfg(&[
            ("RAMEN_OTEL_ENDPOINT", &format!("http://{addr}/")),
            ("RAMEN_OTEL_HEADERS", r#"{"x-api-key": "col-key"}"#),
            ("RAMEN_GROUP", "demo"),
            ("RAMEN_ZONE", "a"),
        ]));
        let mut s = o.begin().expect("on");
        s.adopt(&json!({"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01", "tracestate": "v=1"}));
        s.child("runtime call_tool".into(), SystemTime::now(), true);
        s.attrs.push(("ramen.guardrail.stage", "pre".into()));
        s.end(
            "tools/call",
            Some("echo"),
            "denied",
            vec![("mcp.method", "tools/call".into())],
        );
        o.begin().unwrap().end("tools/list", None, "ok", vec![]);
        o.begin()
            .unwrap()
            .end("tools/call", Some("echo"), "error", vec![]);
        o.flush().await;
        let seen = seen.lock().unwrap();
        let (path, headers, body) = seen.iter().find(|(p, _, _)| p == "/v1/traces").unwrap();
        assert_eq!(
            (path.as_str(), headers["x-api-key"].to_str().unwrap()),
            ("/v1/traces", "col-key")
        );
        let v: Value = serde_json::from_str(body).unwrap();
        let res = &v["resourceSpans"][0]["resource"];
        assert_eq!(attr(res, "service.name"), Some("ramen-node"));
        assert_eq!(
            attr(res, "service.version"),
            Some(env!("CARGO_PKG_VERSION"))
        );
        assert_eq!(
            (attr(res, "ramen.group"), attr(res, "ramen.zone")),
            (Some("demo"), Some("a"))
        );
        let spans = v["resourceSpans"][0]["scopeSpans"][0]["spans"]
            .as_array()
            .unwrap();
        assert_eq!(spans.len(), 4);
        let (child, server) = (&spans[0], &spans[1]);
        assert_eq!(
            (server["name"].as_str(), server["kind"].as_u64()),
            (Some("mcp tools/call"), Some(2))
        );
        assert_eq!(server["traceId"], "0af7651916cd43dd8448eb211c80319c");
        assert_eq!(server["parentSpanId"], "b7ad6b7169203331");
        assert_eq!(server["traceState"], "v=1");
        assert_eq!(attr(server, "ramen.guardrail.stage"), Some("pre"));
        assert!(
            server.get("status").is_none(),
            "a denial is not an error span"
        );
        assert_eq!(
            (child["name"].as_str(), child["kind"].as_u64()),
            (Some("runtime call_tool"), Some(1))
        );
        assert_eq!(
            (&child["traceId"], &child["parentSpanId"]),
            (&server["traceId"], &server["spanId"])
        );
        // a call without a traceparent starts its own trace
        assert!(spans[2].get("parentSpanId").is_none());
        assert_eq!(spans[2]["traceId"].as_str().unwrap().len(), 32);
        assert_eq!(spans[3]["status"]["code"], 2);
        let start: u128 = server["startTimeUnixNano"]
            .as_str()
            .unwrap()
            .parse()
            .unwrap();
        let end: u128 = server["endTimeUnixNano"].as_str().unwrap().parse().unwrap();
        assert!(start > 1_600_000_000_000_000_000 && end >= start);

        let (_, _, body) = seen.iter().find(|(p, _, _)| p == "/v1/metrics").unwrap();
        let v: Value = serde_json::from_str(body).unwrap();
        let metrics = v["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
            .as_array()
            .unwrap();
        let points = |name: &str| {
            let m = metrics.iter().find(|m| m["name"] == name).unwrap();
            assert_eq!(
                (
                    &m["sum"]["isMonotonic"],
                    &m["sum"]["aggregationTemporality"]
                ),
                (&json!(true), &json!(2))
            );
            m["sum"]["dataPoints"].as_array().unwrap().clone()
        };
        assert_eq!(points("ramen.mcp.calls").len(), 3);
        let errors = points("ramen.mcp.errors");
        assert_eq!(errors.len(), 2); // denied + error; never the ok one
        assert!(
            errors
                .iter()
                .all(|p| attr(p, "ramen.status") != Some("ok") && p["asInt"] == "1")
        );
        assert_eq!(attr(&errors[0], "mcp.name"), Some("echo"));
    }

    #[tokio::test]
    async fn an_invalid_traceparent_is_not_adopted() {
        let o = Otel::new(&cfg(&[("RAMEN_OTEL_ENDPOINT", "http://127.0.0.1:1")]));
        let mut s = o.begin().unwrap();
        let own = s.trace_id.clone();
        s.adopt(&json!({"traceparent": "00-00000000000000000000000000000000-b7ad6b7169203331-01"}));
        s.adopt(&json!({"traceparent": "garbage"}));
        s.adopt(&json!({}));
        assert!(s.trace_id == own && s.parent.is_none());
    }

    #[tokio::test]
    async fn an_unreachable_or_failing_collector_never_blocks_and_overflow_is_dropped() {
        // nothing listens here: every post fails, the calls go on
        let o = Otel::new(&cfg(&[("RAMEN_OTEL_ENDPOINT", "http://127.0.0.1:1")]));
        let t0 = Instant::now();
        for _ in 0..(QUEUE * 2 + 10) {
            o.begin()
                .unwrap()
                .end("tools/call", Some("echo"), "ok", vec![]);
        }
        assert!(
            t0.elapsed() < Duration::from_secs(2),
            "ending a span must not wait"
        );
        assert!(o.dropped() > 0, "a full queue drops");
        o.flush().await;
        // a collector that refuses is the same: the batch is gone, the exporter lives
        let (addr, seen) = recorder(500).await;
        o.configure(&cfg(&[("RAMEN_OTEL_ENDPOINT", &format!("http://{addr}"))]));
        o.begin().unwrap().end("tools/list", None, "ok", vec![]);
        o.flush().await;
        assert!(
            seen.lock()
                .unwrap()
                .iter()
                .any(|(p, _, _)| p == "/v1/traces")
        );
        // and turning export off on a reload stops it
        o.configure(&cfg(&[]));
        assert!(o.begin().is_none());
    }

    #[test]
    fn series_are_bounded() {
        let mut c = Counters {
            since: SystemTime::now(),
            calls: BTreeMap::new(),
            dirty: false,
        };
        for i in 0..(MAX_SERIES + 50) {
            c.add(
                "tools/call".into(),
                Some(format!("made-up-{i}")),
                "error".into(),
            );
        }
        assert_eq!(c.calls.len(), MAX_SERIES + 1);
        assert_eq!(
            c.calls[&("tools/call".into(), Some("(other)".into()), "error".into())],
            50
        );
    }
}
