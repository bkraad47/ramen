//! N7: fixed-window-per-minute throttling, two independent Redis instances (§14):
//! `item`  — zone-local, keyed by (ip | token) x (tool/resource/prompt name).
//! `scope` — one instance shared by every zone of this group+env, keyed by (ip | token) x (group, env).
//! Either dimension tripping its configured limit denies the call; 0 = unlimited (that check is skipped).
//! Redis URLs are dialed once at startup (like the bucket path, not live-reloadable); the limit numbers
//! themselves are read fresh from the `Config` passed to `allow` on every call.
use crate::config::Config;
use redis::aio::ConnectionManager;
use std::net::IpAddr;

#[derive(Clone)]
pub struct Throttle {
    item: Option<ConnectionManager>,
    scope: Option<ConnectionManager>,
}

async fn dial(url: &Option<String>) -> Option<ConnectionManager> {
    let client = redis::Client::open(url.as_deref()?).ok()?;
    ConnectionManager::new(client).await.ok()
}

impl Throttle {
    pub async fn connect(cfg: &Config) -> Self {
        Self {
            item: dial(&cfg.redis_item_url).await,
            scope: dial(&cfg.redis_scope_url).await,
        }
    }

    pub fn none() -> Self {
        Self {
            item: None,
            scope: None,
        }
    }

    pub async fn allow(
        &self,
        cfg: &Config,
        ip: IpAddr,
        key_id: Option<&str>,
        item_name: Option<&str>,
    ) -> bool {
        if let (Some(conn), Some(name)) = (&self.item, item_name) {
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
        if let Some(conn) = &self.scope {
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
/// A Redis error fails open: a throttle outage must never take the whole node down with it.
async fn check(conn: &ConnectionManager, key: &str, limit: u64) -> bool {
    if limit == 0 {
        return true;
    }
    let mut conn = conn.clone();
    let result: redis::RedisResult<(i64,)> = redis::pipe()
        .cmd("INCR")
        .arg(key)
        .cmd("EXPIRE")
        .arg(key)
        .arg(60)
        .arg("NX")
        .ignore()
        .query_async(&mut conn)
        .await;
    match result {
        Ok((count,)) => count <= limit as i64,
        Err(_) => true,
    }
}

#[cfg(test)]
mod tests {
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
