//! Bearer keys, admin key, CIDR allowlist and client-IP extraction.
use crate::config::Config;
use axum::http::HeaderMap;
use std::hash::{DefaultHasher, Hash, Hasher};
use std::net::{IpAddr, SocketAddr};

/// Non-secret short id for a key, used only in logs.
pub fn key_id(key: &str) -> String {
    let mut h = DefaultHasher::new();
    key.hash(&mut h);
    format!("{:08x}", h.finish() as u32)
}

pub fn bearer(headers: &HeaderMap) -> Option<&str> {
    headers
        .get("authorization")?
        .to_str()
        .ok()?
        .strip_prefix("Bearer ")
        .map(str::trim)
}

/// Returns the key id when the bearer token is one of the configured keys. No keys = nobody gets in.
pub fn check_key(cfg: &Config, headers: &HeaderMap) -> Option<String> {
    let k = bearer(headers)?;
    cfg.mcp_keys.iter().any(|x| x == k).then(|| key_id(k))
}

pub fn check_admin(cfg: &Config, headers: &HeaderMap) -> bool {
    match (
        &cfg.admin_key,
        headers
            .get("x-ramen-admin-key")
            .and_then(|v| v.to_str().ok()),
    ) {
        (Some(want), Some(got)) => want == got,
        _ => false,
    }
}

pub fn client_ip(cfg: &Config, peer: SocketAddr, headers: &HeaderMap) -> IpAddr {
    if cfg.trust_proxy
        && let Some(xff) = headers.get("x-forwarded-for").and_then(|v| v.to_str().ok())
        && let Some(ip) = xff
            .split(',')
            .next()
            .and_then(|s| s.trim().parse::<IpAddr>().ok())
    {
        return ip;
    }
    peer.ip()
}

fn normalize(ip: IpAddr) -> IpAddr {
    match ip {
        IpAddr::V6(v6) => v6
            .to_ipv4_mapped()
            .map(IpAddr::V4)
            .unwrap_or(IpAddr::V6(v6)),
        v4 => v4,
    }
}

/// `/mcp` allowlist (`RAMEN_ALLOWED_CIDRS`).
pub fn ip_allowed(cfg: &Config, ip: IpAddr) -> bool {
    let ip = normalize(ip);
    cfg.allowed_cidrs.iter().any(|n| n.contains(&ip))
}

/// `/admin/*` allowlist (`RAMEN_ADMIN_CIDRS`, default any) so MCP IP locks cannot break deploys.
pub fn admin_ip_allowed(cfg: &Config, ip: IpAddr) -> bool {
    let ip = normalize(ip);
    cfg.admin_cidrs.iter().any(|n| n.contains(&ip))
}

#[cfg(test)]
mod tests {
    use super::*;
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
    fn h(pairs: &[(&'static str, &str)]) -> HeaderMap {
        let mut m = HeaderMap::new();
        for (k, v) in pairs {
            m.insert(*k, v.parse().unwrap());
        }
        m
    }

    #[test]
    fn keys() {
        let c = cfg(&[("RAMEN_MCP_KEYS", "k1, k2")]);
        assert_eq!(
            check_key(&c, &h(&[("authorization", "Bearer k2")])),
            Some(key_id("k2"))
        );
        assert!(check_key(&c, &h(&[("authorization", "Bearer nope")])).is_none());
        assert!(check_key(&c, &h(&[("authorization", "Basic k1")])).is_none());
        assert!(check_key(&c, &h(&[])).is_none());
        assert!(check_key(&cfg(&[]), &h(&[("authorization", "Bearer k1")])).is_none());
        assert_eq!(key_id("k1").len(), 8);
    }

    #[test]
    fn admin() {
        assert!(check_admin(
            &cfg(&[("RAMEN_ADMIN_KEY", "adm")]),
            &h(&[("x-ramen-admin-key", "adm")])
        ));
        assert!(!check_admin(
            &cfg(&[("RAMEN_ADMIN_KEY", "adm")]),
            &h(&[("x-ramen-admin-key", "x")])
        ));
        assert!(!check_admin(&cfg(&[]), &h(&[("x-ramen-admin-key", "adm")])));
    }

    #[test]
    fn admin_cidrs_independent_of_mcp_lock() {
        let c = cfg(&[("RAMEN_ALLOWED_CIDRS", "203.0.113.0/24")]);
        let console: IpAddr = "10.4.0.7".parse().unwrap();
        assert!(!ip_allowed(&c, console));
        assert!(admin_ip_allowed(&c, console));
        let c = cfg(&[("RAMEN_ADMIN_CIDRS", "10.0.0.0/8")]);
        assert!(admin_ip_allowed(&c, console));
        assert!(!admin_ip_allowed(&c, "203.0.113.9".parse().unwrap()));
    }

    #[test]
    fn ip_and_cidr() {
        let peer: SocketAddr = "10.1.2.3:5".parse().unwrap();
        let xff = h(&[("x-forwarded-for", "203.0.113.9, 10.0.0.1")]);
        assert_eq!(client_ip(&cfg(&[]), peer, &xff), peer.ip());
        assert_eq!(
            client_ip(&cfg(&[("RAMEN_TRUST_PROXY", "1")]), peer, &xff).to_string(),
            "203.0.113.9"
        );
        assert_eq!(
            client_ip(
                &cfg(&[("RAMEN_TRUST_PROXY", "1")]),
                peer,
                &h(&[("x-forwarded-for", "junk")])
            ),
            peer.ip()
        );
        let c = cfg(&[("RAMEN_ALLOWED_CIDRS", "10.0.0.0/8, 192.168.1.0/24")]);
        assert!(ip_allowed(&c, "10.9.9.9".parse().unwrap()));
        assert!(ip_allowed(&c, "::ffff:10.9.9.9".parse().unwrap()));
        assert!(!ip_allowed(&c, "192.168.2.1".parse().unwrap()));
        assert!(!ip_allowed(&c, "::1".parse().unwrap()));
        assert!(ip_allowed(&cfg(&[]), "::1".parse().unwrap()));
    }
}
