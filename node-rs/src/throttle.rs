//! N7: fixed-window-per-minute throttling, two independent Redis instances (§14):
//! `item`  — zone-local, keyed by (ip | token) x (tool/resource/prompt name).
//! `scope` — one instance shared by every zone of this group+env, keyed by (ip | token) x (group, env).
//! Either dimension tripping its configured limit denies the call; 0 = unlimited (that check is skipped).
//! Redis URLs come from the start-up config (not live-reloadable); the limit numbers themselves are read fresh from
//! the `Config` passed to `allow` on every call. §38.3 (D85, F3): each configured Redis is re-checked every
//! `RECHECK` — a Redis that comes back turns throttling on again — and `status` is what `Admin/Metrics` reports.
use crate::config::Config;
use redis::aio::{ConnectionManager, ConnectionManagerConfig};
use serde_json::json;
use std::net::IpAddr;
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::{Arc, RwLock};
use std::time::Duration;
use tokio::sync::Notify;

/// How long the node waits for a Redis at start and on each re-dial. `ConnectionManager::new` retries a host that
/// does not resolve or answer for minutes, and the node does not listen until start-up returns: an unreachable Redis
/// used to keep the pod from ever becoming Ready (kind run, 1.0.0 — the Redis had been deleted). A Redis outage fails
/// open (§14): no throttle until it answers again; the shared task store is chosen at start (memory without one).
const DIAL_TIMEOUT: Duration = Duration::from_secs(5);
/// §38.3: how often a configured Redis is pinged (or, never reached, dialed again).
pub const RECHECK: Duration = Duration::from_secs(30);
/// `RAMEN_REDIS_TIMEOUT_MS`'s default, for a slot built outside a `Config` (tests).
const OP_TIMEOUT: Duration = Duration::from_millis(500);

/// One configured Redis as every user of it sees it: the connection a re-dial replaces in place, whether the last
/// check reached it, and the bound on each operation. The task and consent stores hold the slot, not a copy, so they
/// follow a re-dial — which re-reads `RAMEN_TLS_CA` (§39.1: a CA rotation needs no restart).
struct Shared {
    name: &'static str,
    conn: RwLock<Option<ConnectionManager>>,
    up: AtomicBool,
    /// §39.5a (b35): every operation a call waits for gives up after this (`RAMEN_REDIS_TIMEOUT_MS`).
    timeout: Duration,
    /// Wakes the re-check loop at once when an operation stalls (otherwise every `RECHECK`).
    kick: Arc<Notify>,
}

#[derive(Clone)]
pub struct Slot(Arc<Shared>);

impl Slot {
    fn new(
        name: &'static str,
        conn: Option<ConnectionManager>,
        timeout: Duration,
        kick: Arc<Notify>,
    ) -> Self {
        Slot(Arc::new(Shared {
            name,
            up: AtomicBool::new(conn.is_some()),
            conn: RwLock::new(conn),
            timeout,
            kick,
        }))
    }

    pub fn fixed(conn: ConnectionManager) -> Self {
        Self::new("store", Some(conn), OP_TIMEOUT, Arc::default())
    }

    fn get(&self) -> Option<ConnectionManager> {
        self.0
            .conn
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    fn set(&self, conn: ConnectionManager) {
        *self.0.conn.write().unwrap_or_else(|e| e.into_inner()) = Some(conn);
    }

    fn conn(&self) -> Result<ConnectionManager, String> {
        self.get().ok_or_else(|| "redis: not connected".to_string())
    }

    /// One command, bounded (§39.5a).
    pub async fn cmd<T: redis::FromRedisValue>(&self, c: &redis::Cmd) -> Result<T, String> {
        let mut conn = self.conn()?;
        self.bounded(c.query_async(&mut conn)).await
    }

    /// One pipeline, bounded (§39.5a).
    pub async fn pipe<T: redis::FromRedisValue>(&self, p: &redis::Pipeline) -> Result<T, String> {
        let mut conn = self.conn()?;
        self.bounded(p.query_async(&mut conn)).await
    }

    /// A connection that does not answer within the bound, or fails on the wire, is reported down (the throttle
    /// skips it: fail open) and re-dialed in the background; the caller gets an error now, not after the LB's
    /// backend timeout (GKE b35: one 504 per CA switch, when the chart Redis pod was replaced under the connection).
    async fn bounded<T>(
        &self,
        op: impl std::future::Future<Output = redis::RedisResult<T>>,
    ) -> Result<T, String> {
        let error = match tokio::time::timeout(self.0.timeout, op).await {
            Ok(Ok(v)) => return Ok(v),
            Ok(Err(e)) if !(e.is_io_error() || e.is_timeout() || e.is_connection_dropped()) => {
                return Err(e.to_string()); // an answer (a wrong type, a script error): the connection is fine
            }
            Ok(Err(e)) => e.to_string(),
            Err(_) => format!("no answer within {} ms", self.0.timeout.as_millis()),
        };
        if self.0.up.swap(false, Relaxed) {
            crate::log::emit(
                "warn",
                "redis unreachable: throttling is off for this pod until it answers",
                json!({"redis": self.0.name, "error": error}),
            );
        }
        self.0.kick.notify_one();
        Err(format!("redis: {error}"))
    }
}

/// One configured Redis: its URL and its slot.
struct Link {
    url: Option<String>,
    conn: Slot,
}

impl Link {
    /// The slot a throttle check uses: none while the Redis is unreachable (the check is skipped, fail open).
    fn live(&self) -> Option<&Slot> {
        (self.conn.0.up.load(Relaxed) && self.dialed()).then_some(&self.conn)
    }

    fn dialed(&self) -> bool {
        self.conn.get().is_some()
    }
}

struct Inner {
    item: Link,
    scope: Link,
    /// §39.2: `RAMEN_TLS_CA`, for an in-cluster `rediss://`.
    ca: Option<std::path::PathBuf>,
    /// A whole dial (connect, TLS, auth) at start and on a re-dial.
    dial: Duration,
    /// Each operation and each connection attempt (`RAMEN_REDIS_TIMEOUT_MS`).
    op: Duration,
    kick: Arc<Notify>,
}

#[derive(Clone)]
pub struct Throttle {
    inner: Arc<Inner>,
}

/// §39.3: is this `rediss://` host the chart's Redis (an IP, `localhost`, a Service name: `redis`,
/// `redis.<namespace>` with a hyphenated namespace such as `ramen-system`, `….svc`, `….svc.cluster.local`) rather than
/// a managed instance on a public name?
pub fn in_cluster(host: &str) -> bool {
    let host = host.trim_matches(['[', ']']);
    let labels: Vec<&str> = host.split('.').collect();
    host.parse::<IpAddr>().is_ok()
        || host == "localhost"
        || labels.len() == 1
        || host.ends_with(".svc")
        || host.ends_with(".svc.cluster.local")
        || (labels.len() == 2 && labels[1].contains('-'))
}

/// A client for `url`: `rediss://` to an in-cluster host trusts the install CA bundle (`RAMEN_TLS_CA`, §39.2), any
/// other host the bundled public roots (redis-rs replaces its roots with a given one, so it is one or the other).
pub fn client(url: &str, ca: Option<&std::path::Path>) -> Option<redis::Client> {
    let info: redis::ConnectionInfo = redis::IntoConnectionInfo::into_connection_info(url).ok()?;
    let (redis::ConnectionAddr::TcpTls { host, .. }, Some(ca)) = (&info.addr, ca) else {
        return redis::Client::open(info).ok();
    };
    if !in_cluster(host) {
        return redis::Client::open(info).ok();
    }
    let pem = match std::fs::read(ca) {
        Ok(p) => p,
        Err(e) => {
            crate::log::emit(
                "warn",
                "RAMEN_TLS_CA unreadable: the in-cluster Redis cannot be verified",
                json!({"error": e.to_string()}),
            );
            return None;
        }
    };
    redis::Client::build_with_tls(
        info,
        redis::TlsCertificates {
            client_tls: None,
            root_cert: Some(pem),
        },
    )
    .ok()
}

/// A connection manager whose own reconnects are bounded too: `op` per response, `op` (at least 1 s, a TLS + AUTH
/// handshake to a managed Redis in another region) per connection attempt, two retries (the manager's default, six
/// with back-off, held a call for many seconds after the Redis went away). A call never waits longer than `op` either
/// way: `Slot::bounded` cuts it.
async fn dial_within(
    url: &str,
    ca: Option<&std::path::Path>,
    dial: Duration,
    op: Duration,
) -> Option<ConnectionManager> {
    let client = client(url, ca)?;
    let config = ConnectionManagerConfig::new()
        .set_connection_timeout(op.max(Duration::from_secs(1)))
        .set_response_timeout(op)
        .set_number_of_retries(2);
    match tokio::time::timeout(dial, ConnectionManager::new_with_config(client, config)).await {
        Ok(Ok(conn)) => Some(conn),
        _ => None,
    }
}

impl Throttle {
    pub async fn connect(cfg: &Config) -> Self {
        Self::with_ca(
            cfg.redis_item_url.clone(),
            cfg.redis_scope_url.clone(),
            cfg.tls_ca.clone(),
            DIAL_TIMEOUT,
            cfg.redis_timeout,
        )
        .await
    }

    #[cfg(test)]
    async fn with(item: Option<String>, scope: Option<String>, timeout: Duration) -> Self {
        Self::with_ca(item, scope, None, timeout, timeout).await
    }

    async fn with_ca(
        item: Option<String>,
        scope: Option<String>,
        ca: Option<std::path::PathBuf>,
        dial: Duration,
        op: Duration,
    ) -> Self {
        let ca_ref = ca.as_deref();
        let start = |url: Option<String>| async move {
            let conn = dial_within(url.as_deref()?, ca_ref, dial, op).await;
            if conn.is_none() {
                crate::log::emit(
                    "warn",
                    "redis unreachable at start: throttling is off until it answers (re-checked every 30 s); the shared task store is off for this pod",
                    json!({}),
                );
            }
            conn
        };
        let (ic, sc) = (start(item.clone()).await, start(scope.clone()).await);
        let kick: Arc<Notify> = Arc::default();
        Self {
            inner: Arc::new(Inner {
                item: Link {
                    url: item,
                    conn: Slot::new("item", ic, op, kick.clone()),
                },
                scope: Link {
                    url: scope,
                    conn: Slot::new("scope", sc, op, kick.clone()),
                },
                ca,
                dial,
                op,
                kick,
            }),
        }
    }

    /// §23.5: the Redis the task store shares — the scope instance, else the zone-local one, else none (memory).
    pub fn shared_store(&self) -> Option<Slot> {
        [&self.inner.scope, &self.inner.item]
            .into_iter()
            .find(|l| l.dialed())
            .map(|l| l.conn.clone())
    }

    pub fn none() -> Self {
        let kick: Arc<Notify> = Arc::default();
        let link = |name| Link {
            url: None,
            conn: Slot::new(name, None, OP_TIMEOUT, kick.clone()),
        };
        Self {
            inner: Arc::new(Inner {
                item: link("item"),
                scope: link("scope"),
                ca: None,
                dial: DIAL_TIMEOUT,
                op: OP_TIMEOUT,
                kick: kick.clone(),
            }),
        }
    }

    /// §38.3 `Admin/Metrics.redis`: `off` (no Redis configured), `unreachable` (a configured one did not answer its
    /// last check: throttling is off for it), `ok`.
    pub fn status(&self) -> &'static str {
        let links = [&self.inner.item, &self.inner.scope];
        if links.iter().all(|l| l.url.is_none()) {
            "off"
        } else if links
            .iter()
            .any(|l| l.url.is_some() && !l.conn.0.up.load(Relaxed))
        {
            "unreachable"
        } else {
            "ok"
        }
    }

    /// Pings every configured Redis (dials one never reached); a change of state is one log line.
    pub async fn recheck(&self) {
        for link in [&self.inner.item, &self.inner.scope] {
            let Some(url) = &link.url else { continue };
            let pinged = match link.conn.get() {
                Some(mut c) => {
                    let ping = tokio::time::timeout(self.inner.op, async {
                        let r: redis::RedisResult<String> =
                            redis::cmd("PING").query_async(&mut c).await;
                        r
                    })
                    .await;
                    matches!(ping, Ok(Ok(_)))
                }
                None => false,
            };
            // never reached, or no answer: dial again with the CA bundle re-read (a rotated Redis leaf)
            let ok = pinged
                || match dial_within(
                    url,
                    self.inner.ca.as_deref(),
                    self.inner.dial,
                    self.inner.op,
                )
                .await
                {
                    Some(c) => {
                        link.conn.set(c);
                        true
                    }
                    None => false,
                };
            match (link.conn.0.up.swap(ok, Relaxed), ok) {
                (false, true) => crate::log::emit(
                    "info",
                    "redis reachable: throttling is on for this pod",
                    json!({"redis": link.conn.0.name}),
                ),
                (true, false) => crate::log::emit(
                    "warn",
                    "redis unreachable: throttling is off for this pod until it answers",
                    json!({"redis": link.conn.0.name}),
                ),
                _ => {}
            }
        }
    }

    /// The re-check loop, when a Redis is configured at all: every `RECHECK`, and at once when an operation stalled.
    pub fn spawn_recheck(&self) {
        if self.status() == "off" {
            return;
        }
        let t = self.clone();
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    _ = tokio::time::sleep(RECHECK) => {}
                    _ = t.inner.kick.notified() => {}
                }
                t.recheck().await;
            }
        });
    }

    pub async fn allow(
        &self,
        cfg: &Config,
        ip: IpAddr,
        key_id: Option<&str>,
        item_name: Option<&str>,
    ) -> bool {
        if let (Some(conn), Some(name)) = (self.inner.item.live(), item_name) {
            if !check(
                conn,
                &format!("t:item:ip:{ip}:{name}"),
                cfg.throttle_item_ip,
            )
            .await
            {
                return false;
            }
            if let Some(k) = key_id
                && !check(
                    conn,
                    &format!("t:item:tok:{k}:{name}"),
                    cfg.throttle_item_token,
                )
                .await
            {
                return false;
            }
        }
        if let Some(conn) = self.inner.scope.live() {
            let scope_key = format!("{}:{}", cfg.group, cfg.env);
            if !check(
                conn,
                &format!("t:scope:ip:{ip}:{scope_key}"),
                cfg.throttle_scope_ip,
            )
            .await
            {
                return false;
            }
            if let Some(k) = key_id
                && !check(
                    conn,
                    &format!("t:scope:tok:{k}:{scope_key}"),
                    cfg.throttle_scope_token,
                )
                .await
            {
                return false;
            }
        }
        true
    }
}

/// `INCR`, and `EXPIRE ... NX` only the first time (a fixed window, not renewed by later calls in it).
/// A Redis error or no answer within the bound fails open: a throttle outage must never take the node down with it.
/// A link reported down is skipped by `allow` from the next check on, so a call waits for one bound at most.
async fn check(conn: &Slot, key: &str, limit: u64) -> bool {
    if limit == 0 || !conn.0.up.load(Relaxed) {
        return true;
    }
    let result: Result<(i64,), String> = conn
        .pipe(
            redis::pipe()
                .cmd("INCR")
                .arg(key)
                .cmd("EXPIRE")
                .arg(key)
                .arg(60)
                .arg("NX")
                .ignore(),
        )
        .await;
    match result {
        Ok((count,)) => count <= limit as i64,
        Err(_) => true,
    }
}

#[cfg(test)]
mod tests {
    #[tokio::test]
    async fn an_unreachable_redis_at_start_fails_open_within_the_dial_timeout() {
        let t0 = std::time::Instant::now();
        // a name that does not resolve: the connection manager alone would keep retrying
        let got = super::dial_within(
            "redis://redis.invalid.:6379",
            None,
            super::DIAL_TIMEOUT,
            super::OP_TIMEOUT,
        )
        .await;
        assert!(got.is_none());
        assert!(t0.elapsed() < super::DIAL_TIMEOUT + std::time::Duration::from_secs(2));
    }

    /// A managed Redis (Azure Managed Redis, ElastiCache in-transit encryption, Memorystore TLS) is `rediss://`:
    /// the client must carry TLS, or every such URL fails open silently with "without the tls feature".
    #[tokio::test]
    async fn a_rediss_url_is_dialed_over_tls_not_refused_as_unsupported() {
        let client = redis::Client::open("rediss://:pw@127.0.0.1:1/0").unwrap();
        // one attempt (the manager would retry a refused port for minutes): refused, not "unsupported scheme"
        let err = client
            .get_multiplexed_async_connection()
            .await
            .err()
            .unwrap();
        assert_ne!(err.kind(), redis::ErrorKind::InvalidClientConfig, "{err}");
        assert!(!err.to_string().contains("tls feature"), "{err}");
    }

    /// §39.3 (D86): which `rediss://` hosts are verified against the install CA — the chart's Redis by its Service
    /// names — and which against the public roots (a managed Redis), since a given root replaces redis-rs's roots.
    #[test]
    fn the_install_ca_is_for_in_cluster_redis_only() {
        for h in [
            "redis",
            "redis.ramen-system",
            "redis.ramen-system.svc",
            "redis.ramen-system.svc.cluster.local",
            "localhost",
            "127.0.0.1",
            "[::1]",
        ] {
            assert!(super::in_cluster(h), "{h}");
        }
        for h in [
            "ramen-x.westus2.redis.azure.net",
            "master.ramen.abc.use1.cache.amazonaws.com",
            "10.0.0.5.nip.io",
        ] {
            assert!(!super::in_cluster(h), "{h}");
        }
    }

    #[tokio::test]
    async fn an_in_cluster_rediss_url_is_verified_against_the_install_ca() {
        let ca = crate::testca::TestCa::new();
        let dir = crate::testca::temp_dir("redis");
        let bundle = dir.join("ca.crt");
        std::fs::write(&bundle, ca.pem()).unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let acceptor = ca.acceptor();
        tokio::spawn(async move {
            loop {
                let (s, _) = listener.accept().await.unwrap();
                drop(acceptor.accept(s).await); // a handshake, then nothing: the client sees the stream close
            }
        });
        let url = format!("rediss://:pw@localhost:{port}/0");
        let attempt = |ca: Option<std::path::PathBuf>| {
            let url = url.clone();
            async move {
                let client = super::client(&url, ca.as_deref()).unwrap();
                tokio::time::timeout(
                    std::time::Duration::from_secs(3),
                    client.get_multiplexed_async_connection(),
                )
                .await
                .map(|r| r.err().map(|e| e.to_string()).unwrap_or_default())
                .unwrap_or_default()
            }
        };
        let without = attempt(None).await;
        assert!(without.to_lowercase().contains("certificate"), "{without}");
        let with = attempt(Some(bundle)).await;
        assert!(!with.to_lowercase().contains("certificate"), "{with}");
        // a public host keeps the public roots even with the bundle at hand
        assert!(
            super::client(
                "rediss://:pw@x.redis.azure.net:10000",
                Some(&dir.join("nope"))
            )
            .is_some()
        );
    }

    use super::*;

    fn test_cfg(overrides: &[(&str, &str)]) -> Config {
        let mut m: std::collections::HashMap<String, String> = overrides
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        m.entry("RAMEN_GROUP".into()).or_insert("g".into());
        m.entry("RAMEN_ENV".into()).or_insert("e".into());
        Config::from_map(&m).unwrap()
    }

    #[tokio::test]
    async fn no_redis_configured_always_allows() {
        let t = Throttle::none();
        let cfg = test_cfg(&[("RAMEN_THROTTLE_ITEM_IP", "1")]);
        for _ in 0..5 {
            assert!(
                t.allow(&cfg, "127.0.0.1".parse().unwrap(), Some("k"), Some("tool"))
                    .await
            );
        }
    }

    /// A minimal RESP server: `INCR` counts per key, `PING` answers PONG, anything else (the client's `CLIENT
    /// SETINFO`, `EXPIRE`) `+OK`/`:1`. `stop` drops the listener and every open connection, as a Redis that went away.
    struct FakeRedis {
        port: u16,
        tasks: Arc<std::sync::Mutex<Vec<tokio::task::JoinHandle<()>>>>,
        /// connections numbered below this read but never answer (a Redis pod replaced under an open connection)
        mute_below: Arc<std::sync::atomic::AtomicU64>,
        accepted: Arc<std::sync::atomic::AtomicU64>,
    }

    impl FakeRedis {
        async fn start(port: u16) -> Self {
            use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
            let listener = tokio::net::TcpListener::bind(("127.0.0.1", port))
                .await
                .unwrap();
            let port = listener.local_addr().unwrap().port();
            let tasks: Arc<std::sync::Mutex<Vec<tokio::task::JoinHandle<()>>>> = Arc::default();
            let held = tasks.clone();
            let mute_below = Arc::new(std::sync::atomic::AtomicU64::new(0));
            let accepted = Arc::new(std::sync::atomic::AtomicU64::new(0));
            let (mute, seq) = (mute_below.clone(), accepted.clone());
            let accept = tokio::spawn(async move {
                let counts = Arc::new(std::sync::Mutex::new(std::collections::HashMap::<
                    String,
                    i64,
                >::new()));
                while let Ok((sock, _)) = listener.accept().await {
                    let counts = counts.clone();
                    let (mute, id) = (mute.clone(), seq.fetch_add(1, Relaxed));
                    let conn = tokio::spawn(async move {
                        let (r, mut w) = sock.into_split();
                        let mut r = BufReader::new(r);
                        let mut line = String::new();
                        loop {
                            line.clear();
                            if r.read_line(&mut line).await.unwrap_or(0) == 0 {
                                return;
                            }
                            let n: usize = line.trim_start_matches('*').trim().parse().unwrap_or(0);
                            let mut args = Vec::new();
                            for _ in 0..n {
                                line.clear();
                                r.read_line(&mut line).await.unwrap();
                                let len: usize =
                                    line.trim_start_matches('$').trim().parse().unwrap();
                                let mut buf = vec![0; len + 2];
                                r.read_exact(&mut buf).await.unwrap();
                                args.push(String::from_utf8_lossy(&buf[..len]).to_string());
                            }
                            if id < mute.load(Relaxed) {
                                continue; // read, never answered, never applied
                            }
                            let reply = match args.first().map(|a| a.to_uppercase()).as_deref() {
                                Some("PING") => "+PONG\r\n".to_string(),
                                Some("INCR") => {
                                    let mut c = counts.lock().unwrap();
                                    let v = c.entry(args[1].clone()).or_insert(0);
                                    *v += 1;
                                    format!(":{v}\r\n")
                                }
                                Some("EXPIRE") => ":1\r\n".to_string(),
                                _ => "+OK\r\n".to_string(),
                            };
                            if w.write_all(reply.as_bytes()).await.is_err() {
                                return;
                            }
                        }
                    });
                    held.lock().unwrap().push(conn);
                }
            });
            tasks.lock().unwrap().push(accept);
            Self {
                port,
                tasks,
                mute_below,
                accepted,
            }
        }

        /// Every open connection stops answering; new ones answer.
        fn mute_open(&self) {
            self.mute_below.store(self.accepted.load(Relaxed), Relaxed);
        }

        /// Nothing answers any more, old or new.
        fn mute_all(&self) {
            self.mute_below.store(u64::MAX, Relaxed);
        }

        fn url(&self) -> String {
            format!("redis://127.0.0.1:{}", self.port)
        }

        fn stop(&self) {
            for t in self.tasks.lock().unwrap().drain(..) {
                t.abort();
            }
        }
    }

    fn free_port() -> u16 {
        std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port()
    }

    const QUICK: Duration = Duration::from_millis(500);

    #[tokio::test]
    async fn status_is_off_without_a_redis_and_ok_with_one_that_answers() {
        assert_eq!(Throttle::none().status(), "off");
        let fake = FakeRedis::start(0).await;
        let t = Throttle::with(Some(fake.url()), None, QUICK).await;
        assert_eq!(t.status(), "ok");
        t.recheck().await;
        assert_eq!(t.status(), "ok", "a PING that answers keeps it ok");
        fake.stop();
    }

    #[tokio::test]
    async fn a_redis_that_comes_back_turns_throttling_on_again() {
        let port = free_port();
        let url = format!("redis://127.0.0.1:{port}");
        let t = Throttle::with(None, Some(url), QUICK).await;
        assert_eq!(t.status(), "unreachable");
        let cfg = test_cfg(&[("RAMEN_THROTTLE_SCOPE_IP", "1")]);
        let ip: IpAddr = "10.0.0.9".parse().unwrap();
        for _ in 0..3 {
            assert!(
                t.allow(&cfg, ip, None, None).await,
                "unreachable: fails open"
            );
        }
        t.recheck().await;
        assert_eq!(t.status(), "unreachable", "still nothing on the port");
        let fake = FakeRedis::start(port).await;
        t.recheck().await;
        assert_eq!(t.status(), "ok", "dialed again on the re-check");
        assert!(t.allow(&cfg, ip, None, None).await);
        assert!(
            !t.allow(&cfg, ip, None, None).await,
            "throttling is on again: the 2nd call trips the limit of 1"
        );
        assert!(t.shared_store().is_some());
        fake.stop();
    }

    #[tokio::test]
    async fn a_redis_that_goes_away_after_start_is_reported_and_skipped() {
        let fake = FakeRedis::start(0).await;
        let t = Throttle::with(Some(fake.url()), Some(fake.url()), QUICK).await;
        assert_eq!(t.status(), "ok");
        fake.stop();
        t.recheck().await;
        assert_eq!(t.status(), "unreachable");
        let cfg = test_cfg(&[("RAMEN_THROTTLE_ITEM_IP", "1")]);
        let ip: IpAddr = "10.0.0.8".parse().unwrap();
        for _ in 0..3 {
            assert!(
                t.allow(&cfg, ip, None, Some("tool")).await,
                "fails open, no wait on a dead Redis"
            );
        }
    }

    /// A call made right after the Redis stopped answering (GKE b35: the chart Redis pod replaced at a CA switch, the
    /// open connection silently dead) must not wait for it: the GCP LB's 30 s backend timeout answered 504.
    async fn bounded_allow(t: &Throttle, cfg: &Config, ip: IpAddr) -> (bool, Duration) {
        let t0 = std::time::Instant::now();
        let ok = tokio::time::timeout(
            Duration::from_secs(5),
            t.allow(cfg, ip, Some("k"), Some("tool")),
        )
        .await
        .expect("the call waited on a Redis that does not answer");
        (ok, t0.elapsed())
    }

    #[tokio::test]
    async fn a_redis_that_stops_answering_fails_open_within_the_bound() {
        let fake = FakeRedis::start(0).await;
        let t = Throttle::with(Some(fake.url()), Some(fake.url()), QUICK).await;
        let cfg = test_cfg(&[
            ("RAMEN_THROTTLE_ITEM_IP", "100"),
            ("RAMEN_THROTTLE_SCOPE_TOKEN", "100"),
        ]);
        let ip: IpAddr = "10.0.0.7".parse().unwrap();
        assert!(bounded_allow(&t, &cfg, ip).await.0);
        fake.mute_all();
        let (ok, took) = bounded_allow(&t, &cfg, ip).await;
        assert!(ok, "fails open");
        assert!(took < QUICK * 3, "one bound per Redis at most: {took:?}");
        assert_eq!(t.status(), "unreachable", "reported as the re-check would");
        let (ok, took) = bounded_allow(&t, &cfg, ip).await;
        assert!(
            ok && took < Duration::from_millis(100),
            "skipped while down: {took:?}"
        );
        let store = crate::tasks::Store::Redis(t.shared_store().unwrap());
        let t0 = std::time::Instant::now();
        let got = store.get("t:x").await;
        assert!(got.is_err(), "the task store errors, it does not hang");
        assert!(t0.elapsed() < QUICK * 2, "{:?}", t0.elapsed());
        fake.stop();
    }

    #[tokio::test]
    async fn a_stalled_connection_is_replaced_in_the_background() {
        let fake = FakeRedis::start(0).await;
        let t = Throttle::with(None, Some(fake.url()), QUICK).await;
        t.spawn_recheck();
        let cfg = test_cfg(&[("RAMEN_THROTTLE_SCOPE_IP", "1")]);
        fake.mute_open();
        let ip: IpAddr = "10.0.0.6".parse().unwrap();
        let (ok, took) = bounded_allow(&t, &cfg, ip).await;
        assert!(ok && took < QUICK * 2, "fails open: {took:?}");
        // no 30 s wait: the stall itself starts the re-dial, and new connections answer
        let t0 = std::time::Instant::now();
        while t.status() != "ok" {
            assert!(t0.elapsed() < Duration::from_secs(5), "never re-dialed");
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        assert!(t.allow(&cfg, ip, None, None).await);
        assert!(
            !t.allow(&cfg, ip, None, None).await,
            "throttling is on again"
        );
        let pong: String = t
            .shared_store()
            .unwrap()
            .cmd(&redis::cmd("PING"))
            .await
            .unwrap();
        assert_eq!(pong, "PONG", "the task store follows the re-dial");
        fake.stop();
    }

    #[tokio::test]
    async fn a_redis_that_dies_mid_run_never_holds_a_call() {
        let fake = FakeRedis::start(0).await;
        let t = Throttle::with(Some(fake.url()), None, QUICK).await;
        let cfg = test_cfg(&[("RAMEN_THROTTLE_ITEM_IP", "100")]);
        let ip: IpAddr = "10.0.0.5".parse().unwrap();
        assert!(bounded_allow(&t, &cfg, ip).await.0);
        fake.stop(); // connections closed, port refused: the manager's own reconnect retries with back-off
        for _ in 0..3 {
            let (ok, took) = bounded_allow(&t, &cfg, ip).await;
            assert!(ok && took < QUICK * 2, "{took:?}");
        }
    }

    #[test]
    fn the_operation_bound_is_env_tunable() {
        assert_eq!(test_cfg(&[]).redis_timeout, Duration::from_millis(500));
        let c = test_cfg(&[("RAMEN_REDIS_TIMEOUT_MS", "250")]);
        assert_eq!(c.redis_timeout, Duration::from_millis(250));
        let c = test_cfg(&[("RAMEN_REDIS_TIMEOUT_MS", "1")]);
        assert_eq!(c.redis_timeout, Duration::from_millis(50), "clamped");
    }

    /// §39.1 (b33): a CA rotation restarts the chart Redis with a leaf from the new CA once the two-CA bundle is in
    /// the node's `RAMEN_TLS_CA` file. The live connection drops; the re-check dials again with the bundle re-read
    /// (a `ConnectionManager` keeps the roots it was built with, so its own reconnects would fail for ever), and the
    /// task store, which holds the same slot, follows.
    #[tokio::test]
    async fn a_rotated_redis_leaf_is_trusted_from_the_re_read_bundle() {
        let (old, new) = (crate::testca::TestCa::new(), crate::testca::TestCa::new());
        let dir = crate::testca::temp_dir("redis-rotate");
        let bundle = dir.join("ca.crt");
        std::fs::write(&bundle, old.pem()).unwrap();
        let fake = FakeRedis::start(0).await;
        let backend = fake.port;
        let acceptor = Arc::new(std::sync::RwLock::new(old.acceptor()));
        let conns: Arc<std::sync::Mutex<Vec<tokio::task::JoinHandle<()>>>> = Arc::default();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let (acc, held) = (acceptor.clone(), conns.clone());
        tokio::spawn(async move {
            while let Ok((s, _)) = listener.accept().await {
                let a = acc.read().unwrap().clone();
                held.lock().unwrap().push(tokio::spawn(async move {
                    if let Ok(mut tls) = a.accept(s).await
                        && let Ok(mut up) =
                            tokio::net::TcpStream::connect(("127.0.0.1", backend)).await
                    {
                        let _ = tokio::io::copy_bidirectional(&mut tls, &mut up).await;
                    }
                }));
            }
        });
        let url = format!("rediss://localhost:{port}/0");
        let t = Throttle::with_ca(None, Some(url), Some(bundle.clone()), QUICK, QUICK).await;
        assert_eq!(t.status(), "ok");
        let store = t
            .shared_store()
            .expect("the scope Redis is the shared store");
        // phase 1: the two-CA bundle lands in the file; phase 3: Redis restarts with a new-CA leaf
        std::fs::write(&bundle, format!("{}{}", old.pem(), new.pem())).unwrap();
        *acceptor.write().unwrap() = new.acceptor();
        for c in conns.lock().unwrap().drain(..) {
            c.abort();
        }
        t.recheck().await;
        if t.status() != "ok" {
            t.recheck().await; // the first PING may only notice the dropped connection
        }
        assert_eq!(t.status(), "ok", "re-dialed with the re-read bundle");
        let pong: String = redis::cmd("PING")
            .query_async(&mut store.conn().unwrap())
            .await
            .unwrap();
        assert_eq!(pong, "PONG", "the task store follows the re-dial");
        fake.stop();
    }

    /// Needs a real Redis; set `RAMEN_TEST_REDIS_URL` (e.g. `redis://127.0.0.1:16379`) to run these.
    fn live_url() -> Option<String> {
        std::env::var("RAMEN_TEST_REDIS_URL").ok()
    }

    #[tokio::test]
    async fn item_limit_trips_per_name_and_resets_other_names() {
        let Some(url) = live_url() else {
            eprintln!("skipping: RAMEN_TEST_REDIS_URL not set");
            return;
        };
        let cfg = test_cfg(&[
            ("RAMEN_REDIS_ITEM_URL", &url),
            ("RAMEN_THROTTLE_ITEM_IP", "2"),
        ]);
        let t = Throttle::connect(&cfg).await;
        let ip = "10.0.0.1".parse().unwrap();
        let name = format!("tool-{}", uid());
        assert!(t.allow(&cfg, ip, None, Some(&name)).await);
        assert!(t.allow(&cfg, ip, None, Some(&name)).await);
        assert!(
            !t.allow(&cfg, ip, None, Some(&name)).await,
            "3rd call must trip the limit of 2"
        );
        // a different item name is a different counter, unaffected by the one above
        let other = format!("tool-{}", uid());
        assert!(t.allow(&cfg, ip, None, Some(&other)).await);
    }

    #[tokio::test]
    async fn token_limit_is_independent_of_ip_limit() {
        let Some(url) = live_url() else {
            eprintln!("skipping: RAMEN_TEST_REDIS_URL not set");
            return;
        };
        let cfg = test_cfg(&[
            ("RAMEN_REDIS_ITEM_URL", &url),
            ("RAMEN_THROTTLE_ITEM_IP", "100"),
            ("RAMEN_THROTTLE_ITEM_TOKEN", "1"),
        ]);
        let t = Throttle::connect(&cfg).await;
        let ip = "10.0.0.2".parse().unwrap();
        let name = format!("tool-{}", uid());
        let key = format!("key-{}", uid());
        assert!(t.allow(&cfg, ip, Some(&key), Some(&name)).await);
        assert!(
            !t.allow(&cfg, ip, Some(&key), Some(&name)).await,
            "token limit of 1 must trip on the 2nd call"
        );
    }

    #[tokio::test]
    async fn scope_limit_is_keyed_by_group_and_env_not_item_name() {
        let Some(url) = live_url() else {
            eprintln!("skipping: RAMEN_TEST_REDIS_URL not set");
            return;
        };
        let cfg = test_cfg(&[
            ("RAMEN_REDIS_SCOPE_URL", &url),
            ("RAMEN_THROTTLE_SCOPE_IP", "1"),
            ("RAMEN_GROUP", &format!("grp-{}", uid())),
        ]);
        let t = Throttle::connect(&cfg).await;
        let ip = "10.0.0.3".parse().unwrap();
        assert!(t.allow(&cfg, ip, None, Some("tool-a")).await);
        // a different item name under the SAME group/env still counts against the one scope limit
        assert!(!t.allow(&cfg, ip, None, Some("tool-b")).await);
    }

    #[tokio::test]
    async fn no_item_name_skips_the_item_check_but_not_scope() {
        let Some(url) = live_url() else {
            eprintln!("skipping: RAMEN_TEST_REDIS_URL not set");
            return;
        };
        let cfg = test_cfg(&[
            ("RAMEN_REDIS_ITEM_URL", &url),
            ("RAMEN_REDIS_SCOPE_URL", &url),
            ("RAMEN_THROTTLE_ITEM_IP", "1"),
            ("RAMEN_THROTTLE_SCOPE_IP", "1000"),
            ("RAMEN_GROUP", &format!("grp-{}", uid())),
        ]);
        let t = Throttle::connect(&cfg).await;
        let ip = "10.0.0.4".parse().unwrap();
        for _ in 0..5 {
            assert!(
                t.allow(&cfg, ip, None, None).await,
                "no item name: the item check never applies"
            );
        }
    }

    fn uid() -> String {
        format!(
            "{:x}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        )
    }
}
