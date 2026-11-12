//! Tasks (`io.modelcontextprotocol/tasks`, CONTRACTS §23.5).
//!
//! The runtime runs a task tool on a thread; the node owns the record. The pod that started a task polls the
//! runtime (500 ms) and writes the record to the store, so any pod of the zone answers `tasks/get`; `tasks/update`
//! and `tasks/cancel` are queued in the store and applied by the owner on its next poll. The store is the scope
//! Redis, else the item Redis, else process memory (one pod). A record is bound to the caller binding of §16.2 and
//! to group + zone: anyone else's `taskId` is simply unknown.
use crate::config::Config;
use crate::log::now_iso;
use crate::session::{now, random};
use crate::sidecar::{Hold, RpcErr, Sidecar};
use crate::subs::Subs;
use redis::aio::ConnectionManager;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::Notify;

pub const POLL_INTERVAL_MS: u64 = 2000;
/// The owner rewrites an unchanged record this often, as its heartbeat.
const HEARTBEAT: Duration = Duration::from_secs(5);
/// A non-terminal task whose owner has not written for this long is lost.
const LOST_SECS: u64 = 30;

/// Where task records and the owner's command queue live.
pub enum Store {
    Memory(Mutex<Memory>),
    Redis(ConnectionManager),
}

#[derive(Default)]
pub struct Memory {
    /// key → (record, unix expiry)
    records: HashMap<String, (Value, u64)>,
    queues: HashMap<String, Vec<Value>>,
}

fn queue_key(key: &str) -> String {
    format!("{key}:q")
}

impl Store {
    pub fn memory() -> Self {
        Store::Memory(Mutex::default())
    }

    /// Writes the record, to expire `ttl_secs` from now.
    pub async fn put(&self, key: &str, record: &Value, ttl_secs: u64) -> Result<(), String> {
        match self {
            Store::Memory(m) => {
                let mut m = m.lock().unwrap_or_else(|e| e.into_inner());
                let t = now();
                m.records.retain(|_, (_, exp)| *exp > t);
                m.records
                    .insert(key.to_string(), (record.clone(), t + ttl_secs.max(1)));
                Ok(())
            }
            Store::Redis(c) => redis::cmd("SET")
                .arg(key)
                .arg(record.to_string())
                .arg("EX")
                .arg(ttl_secs.max(1))
                .query_async::<()>(&mut c.clone())
                .await
                .map_err(|e| e.to_string()),
        }
    }

    pub async fn get(&self, key: &str) -> Result<Option<Value>, String> {
        match self {
            Store::Memory(m) => {
                let m = m.lock().unwrap_or_else(|e| e.into_inner());
                Ok(m.records
                    .get(key)
                    .filter(|(_, exp)| *exp > now())
                    .map(|(r, _)| r.clone()))
            }
            Store::Redis(c) => {
                let text: Option<String> = redis::cmd("GET")
                    .arg(key)
                    .query_async(&mut c.clone())
                    .await
                    .map_err(|e| e.to_string())?;
                Ok(text.and_then(|t| serde_json::from_str(&t).ok()))
            }
        }
    }

    /// Queues a command for the task's owner.
    pub async fn push(&self, key: &str, cmd: &Value, ttl_secs: u64) -> Result<(), String> {
        match self {
            Store::Memory(m) => {
                let mut m = m.lock().unwrap_or_else(|e| e.into_inner());
                m.queues
                    .entry(queue_key(key))
                    .or_default()
                    .push(cmd.clone());
                Ok(())
            }
            Store::Redis(c) => redis::pipe()
                .cmd("RPUSH")
                .arg(queue_key(key))
                .arg(cmd.to_string())
                .ignore()
                .cmd("EXPIRE")
                .arg(queue_key(key))
                .arg(ttl_secs.max(1))
                .ignore()
                .query_async::<()>(&mut c.clone())
                .await
                .map_err(|e| e.to_string()),
        }
    }

    /// Takes every queued command, oldest first.
    pub async fn drain(&self, key: &str) -> Result<Vec<Value>, String> {
        match self {
            Store::Memory(m) => {
                let mut m = m.lock().unwrap_or_else(|e| e.into_inner());
                Ok(m.queues.remove(&queue_key(key)).unwrap_or_default())
            }
            Store::Redis(c) => {
                let (items,): (Vec<String>,) = redis::pipe()
                    .atomic()
                    .cmd("LRANGE")
                    .arg(queue_key(key))
                    .arg(0)
                    .arg(-1)
                    .cmd("DEL")
                    .arg(queue_key(key))
                    .ignore()
                    .query_async(&mut c.clone())
                    .await
                    .map_err(|e| e.to_string())?;
                Ok(items
                    .iter()
                    .filter_map(|t| serde_json::from_str(t).ok())
                    .collect())
            }
        }
    }
}

pub struct Tasks {
    store: Store,
    /// This process, as the owner of the tasks it started.
    pod: String,
    /// Tasks this pod is polling, each with the handle that wakes its loop early.
    live: Mutex<HashMap<String, Arc<Notify>>>,
    /// The owner's poll period in ms (500; tests shorten it).
    pub poll_ms: AtomicU64,
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn unknown() -> RpcErr {
    RpcErr::new(-32602, "Unknown task")
}

fn store_err(e: String) -> RpcErr {
    RpcErr::internal(format!("task store unavailable: {e}"))
}

fn terminal(status: &str) -> bool {
    matches!(status, "completed" | "failed" | "cancelled")
}

fn status_of(record: &Value) -> &str {
    record["status"].as_str().unwrap_or("")
}

/// The record as a client sees it (`DetailedTask`): everything but the node's own bookkeeping.
pub fn public(record: &Value) -> Value {
    let mut r = record.clone();
    if let Some(o) = r.as_object_mut() {
        o.remove("x");
    }
    r
}

/// The tool a record belongs to (the access log's `name` on `tasks/*` lines).
pub fn tool_of(record: &Value) -> Option<String> {
    record["x"]["tool"].as_str().map(String::from)
}

fn lose(record: &mut Value) {
    record["status"] = json!("failed");
    record["error"] = json!({"code": -32603, "message": "worker lost"});
    record["lastUpdatedAt"] = json!(now_iso());
    if let Some(o) = record.as_object_mut() {
        o.remove("inputRequests");
    }
}

/// Folds one `runtime.task_poll` answer into the record. `cancelled`: a cancel was just delivered, so anything
/// short of an already-terminal state ends as `cancelled` (the result is discarded, §23.5).
fn apply(record: &mut Value, polled: Result<Value, RpcErr>, cancelled: bool) {
    let Some(o) = record.as_object_mut() else {
        return;
    };
    for k in ["inputRequests", "result", "error", "statusMessage"] {
        o.remove(k);
    }
    let p = match polled {
        Ok(p) => p,
        Err(_) => return lose(record),
    };
    let status = match p["status"].as_str() {
        Some(s) if terminal(s) => s,
        _ if cancelled => "cancelled",
        Some("input_required") => "input_required",
        _ => "working",
    };
    o.insert("status".into(), json!(status));
    match status {
        "input_required" => {
            let asks = p.get("inputRequests").cloned().unwrap_or_else(|| json!({}));
            o.insert("inputRequests".into(), asks);
        }
        "completed" => {
            o.insert(
                "result".into(),
                p.get("result").cloned().unwrap_or(json!({})),
            );
        }
        "failed" => {
            // §23.1: nothing from the legacy range leaves on this era, inlined or not
            let code = p["error"]["code"]
                .as_i64()
                .filter(|c| !(-32019..=-32000).contains(c))
                .unwrap_or(-32603);
            let message = p["error"]["message"].as_str().unwrap_or("task failed");
            o.insert("error".into(), json!({"code": code, "message": message}));
        }
        _ => {}
    }
    if let Some(m) = p.get("statusMessage").filter(|m| m.is_string()) {
        o.insert("statusMessage".into(), m.clone());
    }
}

impl Tasks {
    pub fn new(store: Store) -> Arc<Self> {
        Arc::new(Tasks {
            store,
            pod: hex(&random(8)),
            live: Mutex::default(),
            poll_ms: AtomicU64::new(500),
        })
    }

    /// Tasks running on this pod (`Admin/Metrics` `tasks`, and what `RAMEN_MAX_TASKS` bounds).
    pub fn live(&self) -> usize {
        self.live.lock().unwrap_or_else(|e| e.into_inner()).len()
    }

    fn owns(&self, id: &str) -> bool {
        self.live
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .contains_key(id)
    }

    fn key(cfg: &Config, id: &str) -> String {
        format!("ramen:task:{}:{}:{id}", cfg.group, cfg.zone)
    }

    /// `runtime.task_start`, the first record, and the owner loop. Returns the public record (`working`); the
    /// record is in the store before this returns, so a `tasks/get` for the id resolves at once.
    #[allow(clippy::too_many_arguments)] // one call site; a params struct would only rename these
    pub async fn start(
        self: &Arc<Self>,
        sc: &Arc<Sidecar>,
        subs: &Arc<Subs>,
        cfg: &Config,
        binding: &str,
        name: &str,
        arguments: &Value,
        caps: &Value,
    ) -> Result<Value, RpcErr> {
        let id = hex(&random(16));
        let wake = Arc::new(Notify::new());
        {
            let mut live = self.live.lock().unwrap_or_else(|e| e.into_inner());
            if live.len() >= cfg.max_tasks {
                // `grpc::dispatch_call` refuses this as 429 first; two calls racing for the last slot end here
                return Err(RpcErr::internal("busy: max tasks reached"));
            }
            live.insert(id.clone(), wake.clone());
        }
        let hold = sc.hold();
        let forget = |me: &Self| {
            me.live
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .remove(&id);
        };
        let started = sc
            .call(
                "runtime.task_start",
                json!({"taskId": id, "name": name, "arguments": arguments, "client": {"capabilities": caps}}),
            )
            .await;
        if let Err(e) = started {
            forget(self);
            return Err(e);
        }
        let (t, iso) = (now(), now_iso());
        let record = json!({
            "taskId": id, "status": "working", "createdAt": iso, "lastUpdatedAt": iso,
            "ttlMs": cfg.task_ttl_secs * 1000, "pollIntervalMs": POLL_INTERVAL_MS,
            "x": {"tool": name, "binding": binding, "group": cfg.group, "zone": cfg.zone, "owner": self.pod,
                  "beat": t, "expires": t + cfg.task_ttl_secs},
        });
        let key = Self::key(cfg, &id);
        if let Err(e) = self.store.put(&key, &record, cfg.task_ttl_secs).await {
            forget(self);
            let _ = sc.call("runtime.task_cancel", json!({"taskId": id})).await;
            return Err(store_err(e));
        }
        let out = public(&record);
        tokio::spawn(
            self.clone()
                .own(sc.clone(), subs.clone(), key, record, wake, hold),
        );
        Ok(out)
    }

    /// The owner loop: queued commands, one poll, the record, until the task is terminal or its TTL is up.
    async fn own(
        self: Arc<Self>,
        sc: Arc<Sidecar>,
        subs: Arc<Subs>,
        key: String,
        mut record: Value,
        wake: Arc<Notify>,
        _hold: Hold,
    ) {
        let id = record["taskId"].as_str().unwrap_or("").to_string();
        let expires = record["x"]["expires"].as_u64().unwrap_or(0);
        let arg = || json!({"taskId": id});
        let mut written = Instant::now();
        loop {
            let period = Duration::from_millis(self.poll_ms.load(Relaxed).max(1));
            tokio::select! {
                _ = tokio::time::sleep(period) => {}
                _ = wake.notified() => {}
            }
            let t = now();
            if t >= expires {
                // past its TTL the record is gone from the store; stop the work too
                let _ = sc.call("runtime.task_cancel", arg()).await;
                break;
            }
            let mut cancelled = false;
            for cmd in self.store.drain(&key).await.unwrap_or_default() {
                match cmd["op"].as_str() {
                    Some("update") => {
                        let p = json!({"taskId": id, "inputResponses": cmd["inputResponses"]});
                        let _ = sc.call("runtime.task_update", p).await;
                    }
                    Some("cancel") => {
                        let _ = sc.call("runtime.task_cancel", arg()).await;
                        cancelled = true;
                    }
                    _ => {}
                }
            }
            let before = public(&record);
            apply(
                &mut record,
                sc.call("runtime.task_poll", arg()).await,
                cancelled,
            );
            let changed = public(&record) != before;
            record["x"]["beat"] = json!(t);
            if changed {
                record["lastUpdatedAt"] = json!(now_iso());
            }
            if changed || written.elapsed() >= HEARTBEAT {
                let _ = self.store.put(&key, &record, expires - t).await;
                written = Instant::now();
            }
            if changed {
                subs.task(public(&record));
            }
            if terminal(status_of(&record)) {
                break;
            }
        }
        self.live
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&id);
    }

    /// The full record of the caller's own task in this group and zone; anything else is `Unknown task`. A
    /// non-terminal task whose owner stopped heart-beating is failed here, by whichever pod notices.
    pub async fn get(&self, cfg: &Config, binding: &str, id: &str) -> Result<Value, RpcErr> {
        let key = Self::key(cfg, id);
        let mut record = self
            .store
            .get(&key)
            .await
            .map_err(store_err)?
            .ok_or_else(unknown)?;
        let x = &record["x"];
        let (t, expires) = (now(), x["expires"].as_u64().unwrap_or(0));
        if x["binding"] != binding
            || x["group"] != cfg.group
            || x["zone"] != cfg.zone
            || expires <= t
        {
            return Err(unknown());
        }
        let beat = x["beat"].as_u64().unwrap_or(0);
        if !terminal(status_of(&record)) && !self.owns(id) && t.saturating_sub(beat) > LOST_SECS {
            lose(&mut record);
            let _ = self.store.put(&key, &record, expires - t).await;
        }
        Ok(record)
    }

    /// Queues `cmd` for the owner of the caller's task; a terminal task takes nothing more. Returns the record.
    async fn command(
        &self,
        cfg: &Config,
        binding: &str,
        id: &str,
        cmd: Value,
    ) -> Result<Value, RpcErr> {
        let record = self.get(cfg, binding, id).await?;
        if !terminal(status_of(&record)) {
            self.store
                .push(&Self::key(cfg, id), &cmd, cfg.task_ttl_secs)
                .await
                .map_err(store_err)?;
            let wake = self
                .live
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .get(id)
                .cloned();
            if let Some(w) = wake {
                w.notify_one();
            }
        }
        Ok(record)
    }

    pub async fn update(
        &self,
        cfg: &Config,
        binding: &str,
        id: &str,
        responses: &Value,
    ) -> Result<Value, RpcErr> {
        let cmd = json!({"op": "update", "inputResponses": responses});
        self.command(cfg, binding, id, cmd).await
    }

    pub async fn cancel(&self, cfg: &Config, binding: &str, id: &str) -> Result<Value, RpcErr> {
        self.command(cfg, binding, id, json!({"op": "cancel"}))
            .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> Config {
        let m: HashMap<String, String> = [("RAMEN_GROUP", "g"), ("RAMEN_ZONE", "z")]
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        Config::from_map(&m).unwrap()
    }

    fn record(binding: &str, status: &str, beat: u64, owner: &str) -> Value {
        json!({"taskId": "t1", "status": status, "createdAt": "c", "lastUpdatedAt": "c", "ttlMs": 60000,
               "pollIntervalMs": POLL_INTERVAL_MS,
               "x": {"tool": "slow", "binding": binding, "group": "g", "zone": "z", "owner": owner,
                     "beat": beat, "expires": now() + 60}})
    }

    async fn store_contract(s: &Store, prefix: &str) {
        let k = format!("{prefix}:a");
        assert_eq!(s.get(&k).await.unwrap(), None);
        s.put(&k, &json!({"n": 1}), 60).await.unwrap();
        s.put(&k, &json!({"n": 2}), 60).await.unwrap();
        assert_eq!(s.get(&k).await.unwrap(), Some(json!({"n": 2})));
        assert_eq!(s.drain(&k).await.unwrap(), Vec::<Value>::new());
        s.push(&k, &json!({"op": "update"}), 60).await.unwrap();
        s.push(&k, &json!({"op": "cancel"}), 60).await.unwrap();
        assert_eq!(
            s.drain(&k).await.unwrap(),
            vec![json!({"op": "update"}), json!({"op": "cancel"})]
        );
        assert!(s.drain(&k).await.unwrap().is_empty(), "drained once");
        // a record lives for its TTL and no longer
        let short = format!("{prefix}:short");
        s.put(&short, &json!({}), 1).await.unwrap();
        tokio::time::sleep(Duration::from_millis(2100)).await;
        assert_eq!(s.get(&short).await.unwrap(), None);
        assert!(s.get(&k).await.unwrap().is_some());
    }

    #[tokio::test]
    async fn memory_store_keeps_records_and_queues() {
        store_contract(&Store::memory(), "mem").await;
    }

    /// Needs a real Redis; set `RAMEN_TEST_REDIS_URL` (as the throttle tests do).
    #[tokio::test]
    async fn redis_store_keeps_records_and_queues() {
        let Ok(url) = std::env::var("RAMEN_TEST_REDIS_URL") else {
            eprintln!("skipping: RAMEN_TEST_REDIS_URL not set");
            return;
        };
        let conn = ConnectionManager::new(redis::Client::open(url).unwrap())
            .await
            .unwrap();
        let prefix = format!("ramen:task:test:{}", hex(&random(6)));
        store_contract(&Store::Redis(conn), &prefix).await;
    }

    #[test]
    fn a_poll_answer_folds_into_the_record() {
        let fold = |p: Value, cancelled: bool| {
            let mut r = record("b", "working", 0, "o");
            r["inputRequests"] = json!({"stale": {}});
            apply(&mut r, Ok(p), cancelled);
            public(&r)
        };
        let r = fold(json!({"status": "working"}), false);
        assert_eq!(r["status"], "working");
        assert!(r.get("inputRequests").is_none() && r.get("x").is_none());
        let r = fold(
            json!({"status": "input_required", "inputRequests": {"ok": {"method": "elicitation/create"}}}),
            false,
        );
        assert_eq!(
            (
                r["status"].as_str(),
                r["inputRequests"]["ok"]["method"].as_str()
            ),
            (Some("input_required"), Some("elicitation/create"))
        );
        // a tool error is a completed task whose result says so, never `failed`
        let r = fold(
            json!({"status": "completed", "result": {"content": [], "isError": true}}),
            false,
        );
        assert_eq!(
            (r["status"].as_str(), r["result"]["isError"].as_bool()),
            (Some("completed"), Some(true))
        );
        assert!(r.get("error").is_none());
        let r = fold(
            json!({"status": "failed", "error": {"code": -32004, "message": "boom"}}),
            false,
        );
        assert_eq!(r["error"], json!({"code": -32603, "message": "boom"}));
        assert!(r.get("result").is_none());
        let r = fold(
            json!({"status": "failed", "error": {"code": -32602, "message": "bad"}, "statusMessage": "why"}),
            false,
        );
        assert_eq!(
            (r["error"]["code"].as_i64(), r["statusMessage"].as_str()),
            (Some(-32602), Some("why"))
        );
        // cancel: a running or parked task ends cancelled, a finished one keeps its outcome
        assert_eq!(
            fold(json!({"status": "working"}), true)["status"],
            "cancelled"
        );
        assert_eq!(
            fold(
                json!({"status": "input_required", "inputRequests": {}}),
                true
            )["status"],
            "cancelled"
        );
        assert_eq!(
            fold(json!({"status": "completed", "result": {}}), true)["status"],
            "completed"
        );
        assert_eq!(
            fold(json!({"status": "nonsense"}), false)["status"],
            "working"
        );
        // the runtime no longer knows the task (it died and came back): worker lost
        let mut r = record("b", "working", 0, "o");
        apply(&mut r, Err(RpcErr::new(-32004, "not found: t1")), false);
        assert_eq!(
            (r["status"].as_str(), &r["error"]),
            (
                Some("failed"),
                &json!({"code": -32603, "message": "worker lost"})
            )
        );
    }

    /// §23.5: a task is the caller's, in this group and zone; a silent owner loses it after 30 s.
    #[tokio::test]
    async fn records_are_bound_to_the_caller_and_lost_without_a_heartbeat() {
        let tasks = Tasks::new(Store::memory());
        let c = cfg();
        let key = Tasks::key(&c, "t1");
        assert_eq!(key, "ramen:task:g:z:t1");
        tasks
            .store
            .put(&key, &record("alice", "working", now(), "other-pod"), 60)
            .await
            .unwrap();
        assert_eq!(
            tasks.get(&c, "alice", "t1").await.unwrap()["status"],
            "working"
        );
        assert_eq!(tasks.get(&c, "bob", "t1").await.unwrap_err(), unknown());
        assert_eq!(tasks.get(&c, "alice", "nope").await.unwrap_err(), unknown());
        let mut elsewhere = cfg();
        elsewhere.zone = "other".into();
        assert_eq!(
            tasks.get(&elsewhere, "alice", "t1").await.unwrap_err().code,
            -32602
        );
        // a record written under this zone's key for another zone is still not ours
        let mut foreign = record("alice", "working", now(), "p");
        foreign["x"]["zone"] = json!("other");
        tasks
            .store
            .put(&Tasks::key(&c, "t2"), &foreign, 60)
            .await
            .unwrap();
        assert_eq!(tasks.get(&c, "alice", "t2").await.unwrap_err(), unknown());
        // expired by its own clock, whatever the store says
        let mut old = record("alice", "working", now(), "p");
        old["x"]["expires"] = json!(now());
        tasks
            .store
            .put(&Tasks::key(&c, "t3"), &old, 60)
            .await
            .unwrap();
        assert_eq!(tasks.get(&c, "alice", "t3").await.unwrap_err(), unknown());
        // update/cancel are queued for the owner, and only the caller can queue them
        assert_eq!(
            tasks.update(&c, "bob", "t1", &json!({})).await.unwrap_err(),
            unknown()
        );
        tasks
            .update(&c, "alice", "t1", &json!({"k": {"action": "accept"}}))
            .await
            .unwrap();
        tasks.cancel(&c, "alice", "t1").await.unwrap();
        assert_eq!(
            tasks.store.drain(&key).await.unwrap(),
            vec![
                json!({"op": "update", "inputResponses": {"k": {"action": "accept"}}}),
                json!({"op": "cancel"})
            ]
        );
        // heartbeat 31 s old, owner elsewhere: failed, and written back so every pod agrees
        tasks
            .store
            .put(
                &key,
                &record("alice", "working", now() - 31, "other-pod"),
                60,
            )
            .await
            .unwrap();
        let lost = tasks.get(&c, "alice", "t1").await.unwrap();
        assert_eq!(
            (lost["status"].as_str(), &lost["error"]),
            (
                Some("failed"),
                &json!({"code": -32603, "message": "worker lost"})
            )
        );
        assert_eq!(
            tasks.store.get(&key).await.unwrap().unwrap()["status"],
            "failed"
        );
        // a terminal task takes no more commands, and answering is still fine
        tasks.cancel(&c, "alice", "t1").await.unwrap();
        assert!(tasks.store.drain(&key).await.unwrap().is_empty());
        assert_eq!(tool_of(&lost).as_deref(), Some("slow"));
        assert_eq!(tasks.live(), 0);
    }
}
