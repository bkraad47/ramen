//! Supervises `python -m ramen_runtime --bucket <dir>`: on-demand spawn, id-multiplexed JSON-RPC,
//! idle kill, respawn (with automatic re-`runtime.load`). §23.4: `runtime.progress` notification lines are routed
//! to the call they name; §23.5: a `Hold` keeps the reaper away while a task runs.
use crate::config::Config;
use crate::log::{emit, now_iso, raw};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::process::Stdio;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering::Relaxed};
use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{Mutex, RwLock, oneshot};

#[derive(Debug, Clone, PartialEq)]
pub struct RpcErr {
    pub code: i64,
    pub message: String,
    /// JSON-RPC `error.data` (§23.1: `-32022` supported versions, `-32021` required capabilities, the URI of a
    /// resource that is not there). Never set on a 2025-06-18 answer.
    pub data: Option<Value>,
    /// The access-log `reason` of a refusal (`header_mismatch`, `state_invalid`); never on the wire.
    pub reason: Option<&'static str>,
}

impl RpcErr {
    pub fn new(code: i64, m: impl Into<String>) -> Self {
        RpcErr {
            code,
            message: m.into(),
            data: None,
            reason: None,
        }
    }
    pub fn with_data(mut self, data: Value) -> Self {
        self.data = Some(data);
        self
    }
    pub fn because(mut self, reason: &'static str) -> Self {
        self.reason = Some(reason);
        self
    }
    pub fn internal(m: impl Into<String>) -> Self {
        Self::new(-32603, m)
    }
}

#[derive(Clone)]
pub struct Loaded {
    pub at: String,
    pub result: Value,
}

struct Proc {
    child: Child,
    stdin: ChildStdin,
    alive: Arc<AtomicBool>,
}

type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>>;

/// §23.4: called with the `params` of each `runtime.progress` line a call produces while it runs.
pub type ProgressFn = Arc<dyn Fn(&Value) + Send + Sync>;
type Watchers = Arc<std::sync::Mutex<HashMap<u64, ProgressFn>>>;

/// Removes a call's progress watcher however the call ends.
struct Watch<'a>(&'a Watchers, u64);

impl Drop for Watch<'_> {
    fn drop(&mut self) {
        self.0
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&self.1);
    }
}

/// §23.5: while one of these is alive the sidecar is not idle-killed (a task is running in it).
pub struct Hold(Arc<Sidecar>);

impl Drop for Hold {
    fn drop(&mut self) {
        self.0.holds.fetch_sub(1, Relaxed);
    }
}

pub struct Sidecar {
    cfg: RwLock<Config>,
    proc: Mutex<Option<Proc>>,
    pending: Pending,
    watchers: Watchers,
    holds: AtomicUsize,
    next_id: AtomicU64,
    last_used: Mutex<Instant>,
    loaded: RwLock<Option<Loaded>>,
}

impl Sidecar {
    pub fn new(cfg: Config) -> Arc<Self> {
        Arc::new(Sidecar {
            cfg: RwLock::new(cfg),
            proc: Mutex::new(None),
            pending: Default::default(),
            watchers: Default::default(),
            holds: AtomicUsize::new(0),
            next_id: AtomicU64::new(1),
            last_used: Mutex::new(Instant::now()),
            loaded: RwLock::new(None),
        })
    }

    pub async fn set_config(&self, cfg: Config) {
        *self.cfg.write().await = cfg;
    }

    pub async fn loaded(&self) -> Option<Loaded> {
        self.loaded.read().await.clone()
    }

    #[cfg(test)]
    pub(crate) async fn set_loaded_for_test(&self, result: Value) {
        *self.loaded.write().await = Some(Loaded {
            at: now_iso(),
            result,
        });
    }

    pub fn hold(self: &Arc<Self>) -> Hold {
        self.holds.fetch_add(1, Relaxed);
        Hold(self.clone())
    }

    pub async fn alive(&self) -> bool {
        self.proc
            .lock()
            .await
            .as_ref()
            .is_some_and(|p| p.alive.load(Relaxed))
    }

    /// `runtime.load` for the configured bucket; stores the result for health, lists and metrics.
    pub async fn load(&self) -> Result<Value, RpcErr> {
        let bucket = self.cfg.read().await.bucket.display().to_string();
        let result = self
            .raw_call("runtime.load", json!({"bucket": bucket}), None)
            .await?;
        *self.loaded.write().await = Some(Loaded {
            at: now_iso(),
            result: result.clone(),
        });
        Ok(result)
    }

    /// Any `runtime.*` call; a freshly (re)spawned sidecar is re-loaded first when a load happened before.
    pub async fn call(&self, method: &str, params: Value) -> Result<Value, RpcErr> {
        self.call_watched(method, params, None).await
    }

    /// `call`, with the call's `runtime.progress` lines handed to `progress` as they arrive (§23.4).
    pub async fn call_watched(
        &self,
        method: &str,
        params: Value,
        progress: Option<ProgressFn>,
    ) -> Result<Value, RpcErr> {
        if self.ensure_proc().await? && self.loaded.read().await.is_some() {
            self.load().await?;
        }
        self.raw_call(method, params, progress).await
    }

    async fn raw_call(
        &self,
        method: &str,
        params: Value,
        progress: Option<ProgressFn>,
    ) -> Result<Value, RpcErr> {
        self.ensure_proc().await?;
        let id = self.next_id.fetch_add(1, Relaxed);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(id, tx);
        let _watch = progress.map(|f| {
            self.watchers
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .insert(id, f);
            Watch(&self.watchers, id)
        });
        let line = format!(
            "{}\n",
            json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params})
        );
        {
            let mut guard = self.proc.lock().await;
            let p = guard
                .as_mut()
                .ok_or_else(|| RpcErr::internal("sidecar not running"))?;
            if let Err(e) = p.stdin.write_all(line.as_bytes()).await {
                p.alive.store(false, Relaxed);
                self.pending.lock().await.remove(&id);
                return Err(RpcErr::internal(format!("sidecar write failed: {e}")));
            }
        }
        *self.last_used.lock().await = Instant::now();
        let timeout = Duration::from_secs(self.cfg.read().await.call_timeout_secs);
        let resp = match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(v)) => v,
            Ok(Err(_)) => return Err(RpcErr::internal("sidecar exited during call")),
            Err(_) => {
                self.pending.lock().await.remove(&id);
                self.kill("call timeout").await;
                return Err(RpcErr::internal(format!(
                    "sidecar call timed out after {}s",
                    timeout.as_secs()
                )));
            }
        };
        *self.last_used.lock().await = Instant::now();
        if let Some(e) = resp.get("error") {
            return Err(RpcErr::new(
                e["code"].as_i64().unwrap_or(-32603),
                e["message"].as_str().unwrap_or("sidecar error").to_string(),
            ));
        }
        Ok(resp.get("result").cloned().unwrap_or(Value::Null))
    }

    /// Spawns when missing/dead. Returns true when a new process was started.
    async fn ensure_proc(&self) -> Result<bool, RpcErr> {
        let mut guard = self.proc.lock().await;
        if guard.as_ref().is_some_and(|p| p.alive.load(Relaxed)) {
            return Ok(false);
        }
        let cfg = self.cfg.read().await.clone();
        let mut cmd = Command::new(&cfg.python);
        cmd.args(["-m", "ramen_runtime", "--bucket"])
            .arg(&cfg.bucket)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        // Security review 0.5.0 H1: the runtime executes tenant code, so it must never inherit what makes the
        // NODE trusted — the keys it checks, the secret it signs sessions and verifies tokens with, the admin
        // key, TLS material, the allowlists. Cloud identity, `RAMEN_SECRET_*` and the bucket settings stay.
        for name in std::env::vars_os()
            .map(|(k, _)| k)
            .filter(|k| k.to_str().is_some_and(hidden_from_runtime))
        {
            cmd.env_remove(name);
        }
        if let Some(pp) = &cfg.pythonpath {
            cmd.env("PYTHONPATH", pp);
        }
        match &cfg.bucket_uri {
            Some(uri) => cmd.env("RAMEN_BUCKET_URI", uri),
            None => cmd.env_remove("RAMEN_BUCKET_URI"),
        };
        let mut child = cmd
            .spawn()
            .map_err(|e| RpcErr::internal(format!("spawn {}: {e}", cfg.python)))?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| RpcErr::internal("no stdin"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| RpcErr::internal("no stdout"))?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| RpcErr::internal("no stderr"))?;
        let alive = Arc::new(AtomicBool::new(true));
        emit(
            "info",
            "sidecar spawned",
            json!({"pid": child.id(), "bucket": cfg.bucket}),
        );
        tokio::spawn(read_stdout(
            stdout,
            self.pending.clone(),
            self.watchers.clone(),
            alive.clone(),
        ));
        tokio::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(l)) = lines.next_line().await {
                raw(&l);
            }
        });
        *guard = Some(Proc {
            child,
            stdin,
            alive,
        });
        Ok(true)
    }

    pub async fn kill(&self, why: &str) {
        if let Some(mut p) = self.proc.lock().await.take() {
            p.alive.store(false, Relaxed);
            let _ = p.child.kill().await;
            emit("info", "sidecar stopped", json!({"reason": why}));
        }
        self.pending.lock().await.clear();
    }

    /// Background reaper: kills the sidecar after `idle_secs` without calls.
    pub fn start_reaper(self: &Arc<Self>) {
        let me = self.clone();
        tokio::spawn(async move {
            loop {
                let idle = Duration::from_secs(me.cfg.read().await.idle_secs.max(1));
                tokio::time::sleep(idle.min(Duration::from_secs(5))).await;
                if me.alive().await
                    && me.holds.load(Relaxed) == 0
                    && me.pending.lock().await.is_empty()
                    && me.last_used.lock().await.elapsed() >= idle
                {
                    me.kill("idle").await;
                }
            }
        });
    }
}

async fn read_stdout(
    stdout: tokio::process::ChildStdout,
    pending: Pending,
    watchers: Watchers,
    alive: Arc<AtomicBool>,
) {
    let mut lines = BufReader::new(stdout).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let Ok(v) = serde_json::from_str::<Value>(&line) else {
            emit("warn", "sidecar non-json stdout", json!({"line": line}));
            continue;
        };
        if let Some(id) = v.get("id").and_then(Value::as_u64) {
            if let Some(tx) = pending.lock().await.remove(&id) {
                let _ = tx.send(v);
            }
        } else if let Some(f) = progress_line(&v, &watchers) {
            // §23.4: a line for a call nobody watches (no progressToken, or already answered) is dropped
            f(&v["params"]);
        }
    }
    alive.store(false, Relaxed);
    pending.lock().await.clear();
    emit("warn", "sidecar exited", json!({}));
}

/// `runtime.progress` → the watcher of the call its `params.id` names, if there is one.
fn progress_line(v: &Value, watchers: &Watchers) -> Option<ProgressFn> {
    if v.get("method").and_then(Value::as_str) != Some("runtime.progress") {
        return None;
    }
    let id = v.pointer("/params/id").and_then(Value::as_u64)?;
    watchers
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .get(&id)
        .cloned()
}

/// The node's own credentials and trust settings, which the runtime (tenant code) must not inherit (H1).
pub fn hidden_from_runtime(name: &str) -> bool {
    matches!(
        name,
        "RAMEN_MCP_KEYS"
            | "RAMEN_SESSION_SECRET"
            | "RAMEN_ADMIN_KEY"
            | "RAMEN_TLS_KEY"
            | "RAMEN_TLS_CERT"
            | "RAMEN_ALLOWED_CIDRS"
            | "RAMEN_ADMIN_CIDRS"
            | "RAMEN_ALLOWED_ORIGINS"
            | "RAMEN_OAUTH_ISSUER"
            | "RAMEN_TRUST_PROXY_HOPS"
            | "RAMEN_TRUST_PROXY"
            // §23.5: the task store lives in these; tenant code has no business reading other callers' tasks
            | "RAMEN_REDIS_ITEM_URL"
            | "RAMEN_REDIS_SCOPE_URL"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    /// §23.4: a progress line reaches the watcher of the call it names and nobody else.
    #[test]
    fn progress_lines_are_routed_by_call_id() {
        let watchers: Watchers = Default::default();
        let seen = Arc::new(std::sync::Mutex::new(Vec::<Value>::new()));
        let sink = seen.clone();
        let f: ProgressFn = Arc::new(move |p| sink.lock().unwrap().push(p.clone()));
        watchers.lock().unwrap().insert(7, f);
        let line = |id: u64| json!({"jsonrpc": "2.0", "method": "runtime.progress", "params": {"id": id, "progress": 1}});
        let f = progress_line(&line(7), &watchers).expect("watched");
        f(&line(7)["params"]);
        assert_eq!(seen.lock().unwrap()[0]["id"], 7);
        assert!(progress_line(&line(8), &watchers).is_none());
        assert!(
            progress_line(
                &json!({"method": "runtime.other", "params": {"id": 7}}),
                &watchers
            )
            .is_none()
        );
        assert!(progress_line(&json!({"method": "runtime.progress"}), &watchers).is_none());
        {
            let _w = Watch(&watchers, 7);
        }
        assert!(
            progress_line(&line(7), &watchers).is_none(),
            "the watch ends with the call"
        );
    }

    #[tokio::test]
    async fn a_hold_is_counted_until_dropped() {
        let sc = Sidecar::new(Config::from_map(&Default::default()).unwrap());
        let (a, b) = (sc.hold(), sc.hold());
        assert_eq!(sc.holds.load(Relaxed), 2);
        drop(a);
        drop(b);
        assert_eq!(sc.holds.load(Relaxed), 0);
    }

    #[test]
    fn the_runtime_never_inherits_the_nodes_credentials() {
        for hidden in [
            "RAMEN_MCP_KEYS",
            "RAMEN_SESSION_SECRET",
            "RAMEN_ADMIN_KEY",
            "RAMEN_TLS_KEY",
            "RAMEN_REDIS_ITEM_URL",
            "RAMEN_REDIS_SCOPE_URL",
        ] {
            assert!(hidden_from_runtime(hidden), "{hidden}");
        }
        // what tool code legitimately needs stays: its secrets, the bucket, cloud identity, the interpreter
        for kept in [
            "RAMEN_SECRET_DEMO__TOKEN",
            "RAMEN_BUCKET",
            "RAMEN_BUCKET_URI",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "AWS_REGION",
            "PATH",
            "HOME",
        ] {
            assert!(!hidden_from_runtime(kept), "{kept}");
        }
    }
}
