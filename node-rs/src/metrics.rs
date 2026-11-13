//! Counters behind `Admin/Metrics` (CONTRACTS §3 fields, §11 transport).
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};

#[derive(Default)]
pub struct Metrics {
    pub total: AtomicU64,
    pub errors: AtomicU64,
    /// §23.1: dispatched calls per protocol version served.
    by_protocol: Mutex<BTreeMap<String, u64>>,
    /// §24.1: epoch seconds of the last authenticated, accepted MCP call on either transport; 0 = never.
    pub last_call_at: AtomicU64,
}

impl Metrics {
    pub fn record(&self, ok: bool) {
        self.total.fetch_add(1, Relaxed);
        if !ok {
            self.errors.fetch_add(1, Relaxed);
        }
    }

    /// §24.1: an MCP call got past every guard (credential, throttle, inflight). Health, reflection, `Admin/*` and
    /// denied calls never get here, so they do not keep a zone awake.
    pub fn touch(&self) {
        self.last_call_at.store(crate::session::now(), Relaxed);
    }

    /// §23.1 `calls_by_protocol`. The key is always one of the versions the node serves, never client text.
    pub fn record_protocol(&self, version: &str) {
        if version.is_empty() {
            return;
        }
        let mut m = self.by_protocol.lock().unwrap_or_else(|e| e.into_inner());
        *m.entry(version.to_string()).or_insert(0) += 1;
    }

    // one positional field per metric the console reads; a struct here would just rename the same eight things
    #[allow(clippy::too_many_arguments)]
    pub fn snapshot(
        &self,
        inflight: usize,
        max: usize,
        sidecar_alive: bool,
        loaded_at: Option<String>,
        packages: Value,
        manifest_hash: &str,
        guardrails: Value,
    ) -> Value {
        let pct = inflight * 100 / max.max(1);
        let load = if pct < 30 {
            "low"
        } else if pct > 80 {
            "high"
        } else {
            "even"
        };
        json!({"inflight": inflight, "max_inflight": max, "total": self.total.load(Relaxed), "errors": self.errors.load(Relaxed), "load": load,
               "sidecar_alive": sidecar_alive, "loaded_at": loaded_at, "packages": packages,
               "manifest_hash": manifest_hash, "guardrails": guardrails,
               "last_call_at": self.last_call_at.load(Relaxed),
               "calls_by_protocol": *self.by_protocol.lock().unwrap_or_else(|e| e.into_inner())})
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn load_bands() {
        let m = Metrics::default();
        m.record(true);
        m.record(false);
        let s = m.snapshot(1, 10, false, None, Value::Null, "", json!({}));
        assert_eq!(
            (
                s["total"].as_u64(),
                s["errors"].as_u64(),
                s["load"].as_str()
            ),
            (Some(2), Some(1), Some("low"))
        );
        assert_eq!(
            m.snapshot(5, 10, true, None, Value::Null, "", json!({}))["load"],
            "even"
        );
        assert_eq!(
            m.snapshot(9, 10, true, None, Value::Null, "", json!({}))["load"],
            "high"
        );
    }

    /// §21.7 (0.7.5): the console's autoscaler reads the limit the node runs with next to `inflight`.
    #[test]
    fn max_inflight_is_reported() {
        let m = Metrics::default();
        let s = m.snapshot(3, 48, true, None, Value::Null, "", json!({}));
        assert_eq!(
            (s["inflight"].as_u64(), s["max_inflight"].as_u64()),
            (Some(3), Some(48))
        );
    }

    /// §23.1 (1.0.0): calls are counted per protocol version served.
    #[test]
    fn calls_are_counted_by_protocol() {
        let m = Metrics::default();
        let snap = |m: &Metrics| m.snapshot(0, 1, false, None, Value::Null, "", json!({}));
        assert_eq!(snap(&m)["calls_by_protocol"], json!({}));
        m.record_protocol("2025-06-18");
        m.record_protocol("2026-07-28");
        m.record_protocol("2026-07-28");
        m.record_protocol("");
        assert_eq!(
            snap(&m)["calls_by_protocol"],
            json!({"2025-06-18": 1, "2026-07-28": 2})
        );
    }

    /// C2 (0.7.0): the hash of the manifest this node serves, `""` before the first load.
    #[test]
    fn manifest_hash_is_reported() {
        let m = Metrics::default();
        assert_eq!(
            m.snapshot(0, 1, false, None, Value::Null, "", json!({}))["manifest_hash"],
            ""
        );
        assert_eq!(
            m.snapshot(0, 1, true, None, Value::Null, "ab12", json!({}))["manifest_hash"],
            "ab12"
        );
    }

    /// §24.1 (1.0.0): what the console's idle check reads; 0 until the first accepted call.
    #[test]
    fn last_call_at_is_zero_until_a_call_is_accepted() {
        let m = Metrics::default();
        let snap = |m: &Metrics| m.snapshot(0, 1, false, None, Value::Null, "", json!({}));
        assert_eq!(snap(&m)["last_call_at"], 0);
        m.record(false); // a refused call is counted, but does not keep the zone awake
        assert_eq!(snap(&m)["last_call_at"], 0);
        m.touch();
        let at = snap(&m)["last_call_at"].as_u64().unwrap();
        assert!(at.abs_diff(crate::session::now()) <= 2, "{at}");
    }
}
