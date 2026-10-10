//! Counters behind `Admin/Metrics` (CONTRACTS §3 fields, §11 transport).
use serde_json::{Value, json};
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};

#[derive(Default)]
pub struct Metrics {
    pub total: AtomicU64,
    pub errors: AtomicU64,
}

impl Metrics {
    pub fn record(&self, ok: bool) {
        self.total.fetch_add(1, Relaxed);
        if !ok {
            self.errors.fetch_add(1, Relaxed);
        }
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
        json!({"inflight": inflight, "total": self.total.load(Relaxed), "errors": self.errors.load(Relaxed), "load": load,
               "sidecar_alive": sidecar_alive, "loaded_at": loaded_at, "packages": packages,
               "manifest_hash": manifest_hash, "guardrails": guardrails})
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
}
