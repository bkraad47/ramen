//! Supervises `python -m ramen_runtime --bucket <dir>`: on-demand spawn, id-multiplexed JSON-RPC,
//! idle kill, respawn (with automatic re-`runtime.load`).
use crate::config::Config;
use crate::log::{emit, now_iso, raw};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::process::Stdio;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::Relaxed};
use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{Mutex, RwLock, oneshot};

#[derive(Debug, Clone, PartialEq)]
pub struct RpcErr {
    pub code: i64,
    pub message: String,
}

impl RpcErr {
    pub fn new(code: i64, m: impl Into<String>) -> Self {
        RpcErr {
            code,
            message: m.into(),
        }
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

pub struct Sidecar {
    cfg: RwLock<Config>,
    proc: Mutex<Option<Proc>>,
    pending: Pending,
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
            .raw_call("runtime.load", json!({"bucket": bucket}))
            .await?;
        *self.loaded.write().await = Some(Loaded {
            at: now_iso(),
            result: result.clone(),
        });
        Ok(result)
    }

    /// Any `runtime.*` call; a freshly (re)spawned sidecar is re-loaded first when a load happened before.
    pub async fn call(&self, method: &str, params: Value) -> Result<Value, RpcErr> {
        if self.ensure_proc().await? && self.loaded.read().await.is_some() {
            self.load().await?;
        }
        self.raw_call(method, params).await
    }

    async fn raw_call(&self, method: &str, params: Value) -> Result<Value, RpcErr> {
        self.ensure_proc().await?;
        let id = self.next_id.fetch_add(1, Relaxed);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(id, tx);
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
        tokio::spawn(read_stdout(stdout, self.pending.clone(), alive.clone()));
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
    alive: Arc<AtomicBool>,
) {
    let mut lines = BufReader::new(stdout).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let Ok(v) = serde_json::from_str::<Value>(&line) else {
            emit("warn", "sidecar non-json stdout", json!({"line": line}));
            continue;
        };
        if let Some(id) = v.get("id").and_then(Value::as_u64)
            && let Some(tx) = pending.lock().await.remove(&id)
        {
            let _ = tx.send(v);
        }
    }
    alive.store(false, Relaxed);
    pending.lock().await.clear();
    emit("warn", "sidecar exited", json!({}));
}
