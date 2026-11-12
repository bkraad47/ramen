//! `subscriptions/listen` (CONTRACTS §23.6): the node's one long-lived stream, per pod.
//!
//! A stream is a channel of ready-made SSE frames fed by one task: the ack first, then a `list_changed` per
//! requested kind after each load whose manifest hash differs, `notifications/tasks` for watched tasks this pod
//! owns, a comment line as keep-alive, and the completion result when the node drains. It holds no inflight
//! permit; its own cap is `RAMEN_MAX_SUBSCRIPTIONS`.
use crate::modern::{META_SERVER, META_SUBSCRIPTION, server_info};
use serde_json::{Value, json};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering::Relaxed};
use std::time::Duration;
use tokio::sync::{broadcast, mpsc, watch};

#[derive(Clone, Debug)]
pub enum Event {
    /// A load changed the manifest hash.
    ListChanged,
    /// A task this pod owns changed: its public record (`DetailedTask`).
    Task(Arc<Value>),
}

/// What one stream asked for and the node honours.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Want {
    pub tools: bool,
    pub prompts: bool,
    pub resources: bool,
    pub tasks: Vec<String>,
}

impl Want {
    /// The honoured subset of a request's `params.notifications`; `resourceSubscriptions` never is.
    pub fn of(notifications: &Value, tasks: Vec<String>) -> Self {
        let on = |k: &str| notifications.get(k).and_then(Value::as_bool) == Some(true);
        Want {
            tools: on("toolsListChanged"),
            prompts: on("promptsListChanged"),
            resources: on("resourcesListChanged"),
            tasks,
        }
    }

    /// `params.notifications` of the ack. `with_tasks`: the client declared the tasks extension.
    pub fn ack(&self, with_tasks: bool) -> Value {
        let mut n = serde_json::Map::new();
        for (k, on) in [
            ("toolsListChanged", self.tools),
            ("promptsListChanged", self.prompts),
            ("resourcesListChanged", self.resources),
        ] {
            if on {
                n.insert(k.into(), json!(true));
            }
        }
        if with_tasks {
            n.insert("taskIds".into(), json!(self.tasks));
        }
        Value::Object(n)
    }
}

pub struct Subs {
    events: broadcast::Sender<Event>,
    drain: watch::Sender<bool>,
    open: AtomicUsize,
    /// Keep-alive period in ms (15 s; tests shorten it).
    pub keepalive_ms: AtomicU64,
}

pub fn frame(msg: &Value) -> String {
    format!("data: {msg}\n\n")
}

fn notification(method: &str, id: &Value, extra: Option<&Value>) -> String {
    let mut params = extra.cloned().unwrap_or_else(|| json!({}));
    params["_meta"][META_SUBSCRIPTION] = id.clone();
    frame(&json!({"jsonrpc": "2.0", "method": method, "params": params}))
}

/// Decrements the open-stream count when the stream's task ends, however it ends.
struct Open(Arc<Subs>);

impl Drop for Open {
    fn drop(&mut self) {
        self.0.open.fetch_sub(1, Relaxed);
    }
}

impl Subs {
    pub fn new() -> Arc<Self> {
        Arc::new(Subs {
            events: broadcast::channel(64).0,
            drain: watch::channel(false).0,
            open: AtomicUsize::new(0),
            keepalive_ms: AtomicU64::new(15_000),
        })
    }

    /// Streams open on this pod right now (`Admin/Metrics` `subscriptions`).
    pub fn open(&self) -> usize {
        self.open.load(Relaxed)
    }

    pub fn list_changed(&self) {
        let _ = self.events.send(Event::ListChanged);
    }

    pub fn task(&self, record: Value) {
        let _ = self.events.send(Event::Task(Arc::new(record)));
    }

    /// Shutdown: every stream gets its completion result and ends, so a graceful stop is not held open.
    pub fn drain(&self) {
        self.drain.send_replace(true);
    }

    /// Opens a stream for the request `id`, or `None` at `max` (the caller answers 429). The ack is in the channel
    /// before this returns, so it is the first frame whatever happens next.
    pub fn listen(
        self: &Arc<Self>,
        max: usize,
        id: Value,
        want: Want,
        with_tasks: bool,
    ) -> Option<mpsc::UnboundedReceiver<String>> {
        if self
            .open
            .fetch_update(Relaxed, Relaxed, |n| (n < max).then_some(n + 1))
            .is_err()
        {
            return None;
        }
        let open = Open(self.clone());
        let (tx, rx) = mpsc::unbounded_channel();
        let ack = json!({"notifications": want.ack(with_tasks)});
        let _ = tx.send(notification(
            "notifications/subscriptions/acknowledged",
            &id,
            Some(&ack),
        ));
        let mut events = self.events.subscribe();
        let mut drain = self.drain.subscribe();
        let period = Duration::from_millis(self.keepalive_ms.load(Relaxed).max(1));
        tokio::spawn(async move {
            let _open = open;
            let mut tick = tokio::time::interval_at(tokio::time::Instant::now() + period, period);
            loop {
                let frames: Vec<String> = tokio::select! {
                    _ = tx.closed() => break, // the client went away
                    _ = drain.wait_for(|d| *d) => {
                        let done = json!({"jsonrpc": "2.0", "id": id, "result": {"resultType": "complete",
                            "_meta": {META_SUBSCRIPTION: id, META_SERVER: server_info()}}});
                        let _ = tx.send(frame(&done));
                        break;
                    }
                    _ = tick.tick() => vec![": keep-alive\n\n".to_string()],
                    ev = events.recv() => match ev {
                        // a stream that fell behind missed a change: telling it "the lists changed" is always true enough
                        Ok(Event::ListChanged) | Err(broadcast::error::RecvError::Lagged(_)) => [
                            (want.tools, "notifications/tools/list_changed"),
                            (want.prompts, "notifications/prompts/list_changed"),
                            (want.resources, "notifications/resources/list_changed"),
                        ]
                        .iter()
                        .filter(|(on, _)| *on)
                        .map(|(_, m)| notification(m, &id, None))
                        .collect(),
                        Ok(Event::Task(rec)) => match rec["taskId"].as_str() {
                            Some(t) if want.tasks.iter().any(|w| w == t) => {
                                vec![notification("notifications/tasks", &id, Some(&rec))]
                            }
                            _ => vec![],
                        },
                        Err(broadcast::error::RecvError::Closed) => break,
                    },
                };
                for f in frames {
                    if tx.send(f).is_err() {
                        return;
                    }
                }
            }
        });
        Some(rx)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn data(frame: &str) -> Value {
        serde_json::from_str(frame.strip_prefix("data: ").unwrap().trim_end()).unwrap()
    }

    async fn next(rx: &mut mpsc::UnboundedReceiver<String>) -> String {
        tokio::time::timeout(Duration::from_secs(2), rx.recv())
            .await
            .expect("a frame in time")
            .expect("stream open")
    }

    #[test]
    fn only_requested_true_kinds_are_honoured() {
        let w = Want::of(
            &json!({"toolsListChanged": true, "promptsListChanged": false, "resourceSubscriptions": ["file:///x"]}),
            vec![],
        );
        assert_eq!(
            w,
            Want {
                tools: true,
                ..Default::default()
            }
        );
        assert_eq!(w.ack(false), json!({"toolsListChanged": true}));
        assert_eq!(Want::of(&Value::Null, vec![]).ack(false), json!({}));
        let w = Want::of(&json!({"resourcesListChanged": true}), vec!["t1".into()]);
        assert_eq!(
            w.ack(true),
            json!({"resourcesListChanged": true, "taskIds": ["t1"]})
        );
    }

    #[tokio::test]
    async fn ack_first_then_only_what_was_asked_for_each_tagged() {
        let subs = Subs::new();
        let want = Want {
            prompts: true,
            resources: true,
            tasks: vec!["t1".into()],
            ..Default::default()
        };
        let mut rx = subs.listen(4, json!(41), want, true).unwrap();
        assert_eq!(subs.open(), 1);
        let ack = data(&next(&mut rx).await);
        assert_eq!(ack["method"], "notifications/subscriptions/acknowledged");
        assert_eq!(ack["params"]["_meta"][META_SUBSCRIPTION], 41);
        assert_eq!(
            ack["params"]["notifications"],
            json!({"promptsListChanged": true, "resourcesListChanged": true, "taskIds": ["t1"]})
        );
        subs.list_changed();
        let (a, b) = (data(&next(&mut rx).await), data(&next(&mut rx).await));
        assert_eq!(a["method"], "notifications/prompts/list_changed");
        assert_eq!(b["method"], "notifications/resources/list_changed");
        assert_eq!(a["params"]["_meta"][META_SUBSCRIPTION], 41);
        assert_eq!(b["params"]["_meta"][META_SUBSCRIPTION], 41);
        // tasks: only the watched id
        subs.task(json!({"taskId": "other", "status": "working"}));
        subs.task(json!({"taskId": "t1", "status": "completed"}));
        let t = data(&next(&mut rx).await);
        assert_eq!(t["method"], "notifications/tasks");
        assert_eq!(
            (
                t["params"]["taskId"].as_str(),
                t["params"]["status"].as_str()
            ),
            (Some("t1"), Some("completed"))
        );
        assert_eq!(t["params"]["_meta"][META_SUBSCRIPTION], 41);
        // drain: the completion result, then the stream ends and the count drops
        subs.drain();
        let done = data(&next(&mut rx).await);
        assert_eq!(
            (done["id"].as_i64(), done["result"]["resultType"].as_str()),
            (Some(41), Some("complete"))
        );
        assert_eq!(done["result"]["_meta"][META_SUBSCRIPTION], 41);
        assert!(rx.recv().await.is_none());
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert_eq!(subs.open(), 0);
    }

    #[tokio::test]
    async fn the_cap_holds_and_a_closed_client_frees_its_slot() {
        let subs = Subs::new();
        subs.keepalive_ms.store(20, Relaxed);
        let mut a = subs.listen(1, json!("a"), Want::default(), false).unwrap();
        assert!(subs.listen(1, json!("b"), Want::default(), false).is_none());
        assert_eq!(subs.open(), 1);
        let _ack = next(&mut a).await;
        // nothing requested: a reload sends nothing, the keep-alive comment still comes
        subs.list_changed();
        assert_eq!(next(&mut a).await, ": keep-alive\n\n");
        drop(a);
        tokio::time::sleep(Duration::from_millis(60)).await;
        assert_eq!(subs.open(), 0);
        assert!(subs.listen(1, json!("c"), Want::default(), false).is_some());
        assert!(subs.listen(0, json!("d"), Want::default(), false).is_none());
    }
}
