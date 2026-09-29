//! Bearer keys, admin key (both constant-time), CIDR allowlists and client-IP extraction from gRPC metadata.
use crate::config::Config;
use std::hash::{DefaultHasher, Hash, Hasher};
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use subtle::ConstantTimeEq;
use tonic::metadata::MetadataMap;

/// Non-secret short id for a key, used only in logs.
pub fn key_id(key: &str) -> String {
    let mut h = DefaultHasher::new();
    key.hash(&mut h);
    format!("{:08x}", h.finish() as u32)
}

fn meta<'a>(md: &'a MetadataMap, key: &str) -> Option<&'a str> {
    md.get(key)?.to_str().ok()
}

pub fn bearer(md: &MetadataMap) -> Option<&str> {
    meta(md, "authorization")?
        .strip_prefix("Bearer ")
        .map(str::trim)
}

/// Constant-time equality: same time for every candidate, no early exit on the first mismatching byte.
fn ct_eq(a: &str, b: &str) -> bool {
    a.len() == b.len() && bool::from(a.as_bytes().ct_eq(b.as_bytes()))
}

/// Returns the key id when the bearer token is one of the configured keys. Every key is compared (no
/// short-circuit). No keys = nobody gets in.
pub fn check_key(cfg: &Config, md: &MetadataMap) -> Option<String> {
    let k = bearer(md)?;
    let hit = cfg.mcp_keys.iter().fold(false, |acc, x| ct_eq(x, k) | acc);
    hit.then(|| key_id(k))
}

pub fn check_admin(cfg: &Config, md: &MetadataMap) -> bool {
    match (&cfg.admin_key, meta(md, "x-ramen-admin-key")) {
        (Some(want), Some(got)) => ct_eq(want, got),
        _ => false,
    }
}

/// The peer address, or the client hop of `x-forwarded-for` when proxy trust is on.
///
/// Proxies **append** to `x-forwarded-for`, so only its right-hand end is trustworthy: everything to the
/// left of what the trusted proxies added is supplied by the caller. With `RAMEN_TRUST_PROXY_HOPS = n`
/// (`RAMEN_TRUST_PROXY=1` = 1) the client address is the n-th entry counted from the right — `n - 1`
/// right-hand entries are skipped as the trusted proxies' own hops — and the left-hand entries are ignored.
/// Anything else falls back to the peer address: proxy trust off, no header, fewer than `n` entries, or an
/// entry at that position that is not an address. So a caller can never choose the address that is checked,
/// and a wrong `n` fails closed (a too-large `n` on a short header lands on the peer, which for a
/// behind-the-LB deployment is the proxy, not a client range). An unknown peer is `0.0.0.0`, which only the
/// default any-CIDR allows.
pub fn client_ip(cfg: &Config, peer: Option<SocketAddr>, md: &MetadataMap) -> IpAddr {
    let peer_ip = || peer.map_or(IpAddr::V4(Ipv4Addr::UNSPECIFIED), |p| p.ip());
    if cfg.trust_proxy_hops == 0 {
        return peer_ip();
    }
    let Some(xff) = meta(md, "x-forwarded-for") else {
        return peer_ip();
    };
    let hops: Vec<&str> = xff.split(',').collect();
    hops.len()
        .checked_sub(cfg.trust_proxy_hops)
        .and_then(|i| hops[i].trim().parse::<IpAddr>().ok())
        .unwrap_or_else(peer_ip)
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

/// `Mcp/*` allowlist (`RAMEN_ALLOWED_CIDRS`).
pub fn ip_allowed(cfg: &Config, ip: IpAddr) -> bool {
    let ip = normalize(ip);
    cfg.allowed_cidrs.iter().any(|n| n.contains(&ip))
}

/// `Admin/*` allowlist (`RAMEN_ADMIN_CIDRS`, default any) so MCP IP locks cannot break deploys.
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
    fn h(pairs: &[(&'static str, &str)]) -> MetadataMap {
        let mut m = MetadataMap::new();
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
        assert_eq!(
            check_key(&c, &h(&[("authorization", "Bearer k1")])),
            Some(key_id("k1"))
        );
        assert!(check_key(&c, &h(&[("authorization", "Bearer nope")])).is_none());
        assert!(check_key(&c, &h(&[("authorization", "Bearer k")])).is_none());
        assert!(check_key(&c, &h(&[("authorization", "Bearer k11")])).is_none());
        assert!(check_key(&c, &h(&[("authorization", "Basic k1")])).is_none());
        assert!(check_key(&c, &h(&[])).is_none());
        assert!(check_key(&cfg(&[]), &h(&[("authorization", "Bearer k1")])).is_none());
        assert_eq!(key_id("k1").len(), 8);
        assert!(ct_eq("abc", "abc") && !ct_eq("abc", "abd") && !ct_eq("ab", "abc"));
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
        assert!(!check_admin(&cfg(&[("RAMEN_ADMIN_KEY", "adm")]), &h(&[])));
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
        assert_eq!(client_ip(&cfg(&[]), Some(peer), &xff), peer.ip());
        // one hop = the right-most entry, the one the trusted proxy appended
        assert_eq!(
            client_ip(&cfg(&[("RAMEN_TRUST_PROXY", "1")]), Some(peer), &xff).to_string(),
            "10.0.0.1"
        );
        assert_eq!(
            client_ip(
                &cfg(&[("RAMEN_TRUST_PROXY", "1")]),
                Some(peer),
                &h(&[("x-forwarded-for", "junk")])
            ),
            peer.ip()
        );
        assert_eq!(
            client_ip(&cfg(&[]), None, &h(&[])),
            IpAddr::V4(Ipv4Addr::UNSPECIFIED)
        );
        let c = cfg(&[("RAMEN_ALLOWED_CIDRS", "10.0.0.0/8, 192.168.1.0/24")]);
        assert!(ip_allowed(&c, "10.9.9.9".parse().unwrap()));
        assert!(ip_allowed(&c, "::ffff:10.9.9.9".parse().unwrap()));
        assert!(!ip_allowed(&c, "192.168.2.1".parse().unwrap()));
        assert!(!ip_allowed(&c, "::1".parse().unwrap()));
        assert!(!ip_allowed(&c, IpAddr::V4(Ipv4Addr::UNSPECIFIED)));
        assert!(ip_allowed(&cfg(&[]), "::1".parse().unwrap()));
    }

    /// `x-forwarded-for` is counted from the right, because that is the end a proxy appends to: a caller
    /// can prepend anything it likes and never move the entry the node reads (claims review rows 14/36).
    #[test]
    fn forwarded_for_is_counted_from_the_right() {
        let peer: SocketAddr = "35.191.0.7:5".parse().unwrap(); // a GCP front end, not a client range
        let client = "203.0.113.9";
        // GCP external ALB appends "<client>, <lb>"; AWS ALB appends "<client>".
        let gcp = cfg(&[("RAMEN_TRUST_PROXY_HOPS", "2")]);
        let aws = cfg(&[("RAMEN_TRUST_PROXY_HOPS", "1")]);
        let spoofed = format!("10.0.0.1, 192.0.2.1, {client}");
        assert_eq!(
            client_ip(&aws, Some(peer), &h(&[("x-forwarded-for", &spoofed)])).to_string(),
            client
        );
        assert_eq!(
            client_ip(
                &gcp,
                Some(peer),
                &h(&[("x-forwarded-for", &format!("{spoofed}, 130.211.0.5"))])
            )
            .to_string(),
            client
        );
        // A caller-chosen left-most value is never what gets checked.
        for c in [&gcp, &aws] {
            assert_ne!(
                client_ip(c, Some(peer), &h(&[("x-forwarded-for", &spoofed)])).to_string(),
                "10.0.0.1"
            );
        }
        // Too short, empty or malformed at the counted position → the peer address, never a caller value.
        for bad in ["203.0.113.9", "", " , 203.0.113.9", "junk, 203.0.113.9"] {
            assert_eq!(
                client_ip(&gcp, Some(peer), &h(&[("x-forwarded-for", bad)])),
                peer.ip(),
                "gcp hop count unsatisfied by {bad:?}"
            );
        }
        assert_eq!(client_ip(&gcp, Some(peer), &h(&[])), peer.ip());
        // Proxy trust off (the default) ignores the header entirely.
        let off = cfg(&[("RAMEN_TRUST_PROXY_HOPS", "0")]);
        assert_eq!(off.trust_proxy_hops, 0);
        assert_eq!(cfg(&[]).trust_proxy_hops, 0);
        for c in [&off, &cfg(&[])] {
            assert_eq!(
                client_ip(c, Some(peer), &h(&[("x-forwarded-for", &spoofed)])),
                peer.ip()
            );
        }
        // An explicit hop count wins over the legacy switch, in both directions.
        assert_eq!(
            cfg(&[("RAMEN_TRUST_PROXY", "1"), ("RAMEN_TRUST_PROXY_HOPS", "0")]).trust_proxy_hops,
            0
        );
        assert_eq!(
            cfg(&[("RAMEN_TRUST_PROXY", "0"), ("RAMEN_TRUST_PROXY_HOPS", "2")]).trust_proxy_hops,
            2
        );
        assert_eq!(cfg(&[("RAMEN_TRUST_PROXY", "true")]).trust_proxy_hops, 1);
    }
}
