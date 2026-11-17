//! Counters behind `Admin/Metrics` (CONTRACTS §3 fields, §11 transport).
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};

/// §35.1 (D83): the window the console's p50 trigger reads, in seconds.
pub const WINDOW_SECS: u64 = 60;
/// Samples kept per second; beyond it the newest overwrite the oldest of that second (a bounded ring per bucket).
pub const SAMPLE_CAP: usize = 4096;

/// One second of latencies: the epoch second it holds, its samples (ms) and how many were recorded in it.
#[derive(Default, Clone)]
struct Bucket {
    sec: u64,
    ms: Vec<u32>,
    n: usize,
}

#[derive(Default)]
pub struct Metrics {
    pub total: AtomicU64,
    pub errors: AtomicU64,
    /// §23.1: dispatched calls per protocol version served.
    by_protocol: Mutex<BTreeMap<String, u64>>,
    /// §24.1: epoch seconds of the last authenticated, accepted MCP call on either transport; 0 = never.
    pub last_call_at: AtomicU64,
    /// §35.1: a ring of `WINDOW_SECS` per-second buckets of dispatched-call latencies; quantiles on read.
    latency: Mutex<Vec<Bucket>>,
}

impl Metrics {
    /// §35.1: the wall time of one dispatched call (parse → answer), on either transport.
    pub fn record_latency(&self, took: std::time::Duration) {
        self.record_latency_at(
            took.as_millis().min(u32::MAX as u128) as u32,
            crate::session::now(),
        );
    }

    pub fn record_latency_at(&self, ms: u32, now_sec: u64) {
        let mut ring = self.latency.lock().unwrap_or_else(|e| e.into_inner());
        if ring.is_empty() {
            ring.resize(WINDOW_SECS as usize, Bucket::default());
        }
        let b = &mut ring[(now_sec % WINDOW_SECS) as usize];
        if b.sec != now_sec {
            *b = Bucket {
                sec: now_sec,
                ms: Vec::new(),
                n: 0,
            };
        }
        if b.ms.len() < SAMPLE_CAP {
            b.ms.push(ms);
        } else {
            b.ms[b.n % SAMPLE_CAP] = ms;
        }
        b.n += 1;
    }

    /// `{latency_p50_ms, latency_p95_ms, latency_n}` over the buckets younger than `WINDOW_SECS`; nulls and 0 when
    /// no call was dispatched in the window.
    pub fn quantiles_at(&self, now_sec: u64) -> Value {
        let ring = self.latency.lock().unwrap_or_else(|e| e.into_inner());
        let mut all: Vec<u32> = ring
            .iter()
            .filter(|b| b.n > 0 && b.sec + WINDOW_SECS > now_sec && b.sec <= now_sec)
            .flat_map(|b| b.ms.iter().copied())
            .collect();
        if all.is_empty() {
            return json!({"latency_p50_ms": null, "latency_p95_ms": null, "latency_n": 0});
        }
        all.sort_unstable();
        // nearest-rank: the value at ceil(q × n) - 1
        let rank = |q: f64| all[((q * all.len() as f64).ceil() as usize).clamp(1, all.len()) - 1];
        json!({"latency_p50_ms": rank(0.5), "latency_p95_ms": rank(0.95), "latency_n": all.len()})
    }

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
        let mut m = json!({"inflight": inflight, "max_inflight": max, "total": self.total.load(Relaxed), "errors": self.errors.load(Relaxed), "load": load,
               "sidecar_alive": sidecar_alive, "loaded_at": loaded_at, "packages": packages,
               "manifest_hash": manifest_hash, "guardrails": guardrails,
               "last_call_at": self.last_call_at.load(Relaxed),
               "calls_by_protocol": *self.by_protocol.lock().unwrap_or_else(|e| e.into_inner())});
        // §35.1 (D83): p50/p95 of the last minute's dispatched calls, for the console's latency trigger
        if let (Value::Object(o), Value::Object(q)) =
            (&mut m, self.quantiles_at(crate::session::now()))
        {
            o.extend(q);
        }
        m
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

    /// §35.1 (1.0.0c, D83): p50/p95 of the dispatched calls of the last 60 s, what the autoscaler's latency
    /// trigger reads. Null before the first call; samples older than the window fall out.
    #[test]
    fn latency_quantiles_cover_the_last_minute_only() {
        let m = Metrics::default();
        let snap = |m: &Metrics, at: u64| m.quantiles_at(at);
        assert_eq!(
            snap(&m, 1000),
            json!({"latency_p50_ms": null, "latency_p95_ms": null, "latency_n": 0})
        );
        for ms in [100u32, 200, 300, 400, 500, 600, 700, 800, 900, 1000] {
            m.record_latency_at(ms, 1000);
        }
        let s = snap(&m, 1000);
        assert_eq!(
            (
                s["latency_p50_ms"].as_u64(),
                s["latency_p95_ms"].as_u64(),
                s["latency_n"].as_u64()
            ),
            (Some(500), Some(1000), Some(10))
        );
        m.record_latency_at(5, 1030); // a second sample set half a minute later
        let s = snap(&m, 1059);
        assert_eq!(s["latency_n"], 11);
        let s = snap(&m, 1060); // the 1000 s bucket is now older than 60 s
        assert_eq!(
            (s["latency_p50_ms"].as_u64(), s["latency_n"].as_u64()),
            (Some(5), Some(1))
        );
        assert_eq!(snap(&m, 1200)["latency_n"], 0);
    }

    /// A bucket keeps a bounded number of samples; beyond that the newest overwrite the oldest of that second.
    #[test]
    fn a_second_holds_at_most_the_cap() {
        let m = Metrics::default();
        for i in 0..(SAMPLE_CAP as u32 + 10) {
            m.record_latency_at(i, 7);
        }
        let s = m.quantiles_at(7);
        assert_eq!(s["latency_n"].as_u64(), Some(SAMPLE_CAP as u64));
        // the kept samples are 10..4106: the median moved up by the 10 overwritten
        assert_eq!(
            s["latency_p50_ms"].as_u64(),
            Some(SAMPLE_CAP as u64 / 2 + 9)
        );
    }

    /// The snapshot the console polls carries the three fields next to `total`.
    #[test]
    fn snapshot_carries_latency_fields() {
        let m = Metrics::default();
        let s = m.snapshot(0, 1, false, None, Value::Null, "", json!({}));
        assert!(s["latency_p50_ms"].is_null() && s["latency_n"] == 0);
        m.record_latency(std::time::Duration::from_millis(42));
        let s = m.snapshot(0, 1, false, None, Value::Null, "", json!({}));
        assert_eq!(
            (s["latency_p50_ms"].as_u64(), s["latency_n"].as_u64()),
            (Some(42), Some(1))
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
