//! Stateless `Mcp-Session-Id`s (CONTRACTS §16.2, D33).
//!
//! `<nonce>.<expiry>.<hash12>.<mac>` with `mac = HMAC-SHA256(secret, nonce ‖ expiry ‖ hash12 ‖ key_id)`. Any pod
//! holding the zone's secret verifies an id without state, so there is no affinity, no store, and a rollout keeps
//! sessions alive. The MAC binds the id to the credential that minted it: presented under another key it is simply
//! invalid, so a stolen id reveals nothing and cannot be replayed with a different key.
//!
//! C11 (0.7.2): `hash12` is the first 12 hex of the manifest hash the node served at `initialize` (`""` before any
//! load). A request whose session carries another hash than the pod's current one is how the pod knows the client
//! has not seen the current tool list (http.rs). Three-part ids minted by 0.7.1 still verify, as `hash12 = ""`.
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD as B64;
use ring::{hmac, rand};
use std::collections::HashSet;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

const NONCE_BYTES: usize = 16;

/// The zone's session secret: configured, or generated once per process when it is not.
pub struct Sessions {
    key: hmac::Key,
    ttl_secs: u64,
    pub generated: bool,
}

/// Why an id was refused; every variant is answered `404` (the spec's "re-initialize" signal), never `401`.
#[derive(Debug, PartialEq, Eq)]
pub enum Invalid {
    Malformed,
    Expired,
    BadMac,
}

/// What a verified id says: its nonce (what per-session bookkeeping keys on) and the manifest hash prefix it
/// was minted with.
#[derive(Debug, PartialEq, Eq)]
pub struct Session {
    pub nonce: String,
    pub hash12: String,
}

impl Sessions {
    pub fn new(secret: Option<&str>, ttl_secs: u64) -> Self {
        let (bytes, generated) = match secret {
            Some(s) => (s.as_bytes().to_vec(), false),
            None => (random(32), true),
        };
        Self {
            key: hmac::Key::new(hmac::HMAC_SHA256, &bytes),
            ttl_secs,
            generated,
        }
    }

    /// A fresh id bound to `key_id` and stamped with `hash12`, expiring `ttl_secs` from now.
    pub fn issue(&self, key_id: &str, hash12: &str) -> String {
        self.issue_at(key_id, hash12, now() + self.ttl_secs)
    }

    fn issue_at(&self, key_id: &str, hash12: &str, expiry: u64) -> String {
        let nonce = B64.encode(random(NONCE_BYTES));
        let mac = B64.encode(hmac::sign(
            &self.key,
            message(&nonce, expiry, Some(hash12), key_id).as_bytes(),
        ));
        format!("{nonce}.{expiry}.{hash12}.{mac}")
    }

    /// The 0.7.1 three-part shape, for the compatibility tests.
    #[cfg(test)]
    pub(crate) fn issue_legacy(&self, key_id: &str) -> String {
        let nonce = B64.encode(random(NONCE_BYTES));
        let expiry = now() + self.ttl_secs;
        let mac = B64.encode(hmac::sign(
            &self.key,
            message(&nonce, expiry, None, key_id).as_bytes(),
        ));
        format!("{nonce}.{expiry}.{mac}")
    }

    /// Valid for `key_id` and not expired. A three-part id (0.7.1) verifies with its own message and reads as
    /// `hash12 = ""`.
    pub fn verify(&self, id: &str, key_id: &str) -> Result<Session, Invalid> {
        let parts: Vec<&str> = id.split('.').collect();
        let (nonce, exp, hash12, mac) = match parts[..] {
            [n, e, m] => (n, e, None, m),
            [n, e, h, m] => (n, e, Some(h), m),
            _ => return Err(Invalid::Malformed),
        };
        let expiry: u64 = exp.parse().map_err(|_| Invalid::Malformed)?;
        let tag = B64.decode(mac).map_err(|_| Invalid::Malformed)?;
        // MAC first, so an attacker cannot learn whether a forged id is "merely" expired.
        hmac::verify(
            &self.key,
            message(nonce, expiry, hash12, key_id).as_bytes(),
            &tag,
        )
        .map_err(|_| Invalid::BadMac)?;
        if expiry <= now() {
            return Err(Invalid::Expired);
        }
        Ok(Session {
            nonce: nonce.to_string(),
            hash12: hash12.unwrap_or("").to_string(),
        })
    }

    /// HKDF-style derivation of the OAuth token key from the same secret (§16.3), so one deploy secret covers both.
    pub fn derived_key(&self, label: &str) -> hmac::Key {
        hmac::Key::new(
            hmac::HMAC_SHA256,
            hmac::sign(&self.key, label.as_bytes()).as_ref(),
        )
    }
}

/// `None` is the 0.7.1 three-part message; a four-part id always includes its (possibly empty) `hash12`, so the
/// two shapes never share a MAC.
fn message(nonce: &str, expiry: u64, hash12: Option<&str>, key_id: &str) -> String {
    match hash12 {
        None => format!("{nonce}\n{expiry}\n{key_id}"),
        Some(h) => format!("{nonce}\n{expiry}\n{h}\n{key_id}"),
    }
}

/// C11: which sessions this pod has already told about the current manifest — a bounded in-process set (two
/// generations of `cap / 2`, the older one dropped whole when the newer fills: O(1), at most `cap` entries, and
/// nothing is ever forgotten before `cap / 2` newer entries arrived). Keyed by `<nonce>.<hash12>` by the caller,
/// so a session is notified once per manifest version it has not seen.
pub struct Notified {
    cap: usize,
    gens: Mutex<(HashSet<String>, HashSet<String>)>,
}

impl Notified {
    pub fn new(cap: usize) -> Self {
        Self {
            cap: cap.max(2),
            gens: Mutex::new((HashSet::new(), HashSet::new())),
        }
    }

    /// `true` the first time `key` is seen (the caller should notify), `false` afterwards.
    pub fn first_time(&self, key: &str) -> bool {
        let mut g = self.gens.lock().unwrap_or_else(|e| e.into_inner());
        let (cur, prev) = &mut *g;
        if cur.contains(key) {
            return false;
        }
        if prev.remove(key) {
            cur.insert(key.to_string()); // refresh: a live session stays remembered
            return false;
        }
        if cur.len() >= self.cap / 2 {
            *prev = std::mem::take(cur);
        }
        cur.insert(key.to_string());
        true
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        let g = self.gens.lock().unwrap_or_else(|e| e.into_inner());
        g.0.len() + g.1.len()
    }
}

pub fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

pub fn random(n: usize) -> Vec<u8> {
    use ring::rand::SecureRandom;
    let mut buf = vec![0u8; n];
    rand::SystemRandom::new()
        .fill(&mut buf)
        .expect("system randomness");
    buf
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_binds_the_credential_and_carries_the_hash() {
        let s = Sessions::new(Some("zone-secret"), 60);
        let id = s.issue("key-a", "0123456789ab");
        let v = s.verify(&id, "key-a").unwrap();
        assert_eq!(v.hash12, "0123456789ab");
        assert_eq!(id.split('.').count(), 4);
        assert!(id.starts_with(&format!("{}.", v.nonce)));
        assert_eq!(s.verify(&id, "key-b").unwrap_err(), Invalid::BadMac); // another key cannot reuse it (§16.2)
        assert_ne!(s.issue("key-a", "0123456789ab"), id); // fresh nonce every time
        // before any load the stamp is empty, and the id still has four parts
        let id = s.issue("key-a", "");
        assert_eq!(id.split('.').count(), 4);
        assert_eq!(s.verify(&id, "key-a").unwrap().hash12, "");
    }

    /// C11: a 0.7.1 id (three parts) is still a valid session during a rolling upgrade, read as `hash12 = ""`.
    #[test]
    fn three_part_ids_from_0_7_1_still_verify() {
        let s = Sessions::new(Some("zone-secret"), 60);
        let old = s.issue_legacy("key-a");
        assert_eq!(old.split('.').count(), 3);
        let v = s.verify(&old, "key-a").unwrap();
        assert_eq!(v.hash12, "");
        assert_eq!(s.verify(&old, "key-b").unwrap_err(), Invalid::BadMac);
        // the two shapes never share a MAC: an old id cannot be promoted to a new one, or the reverse
        let (nonce, rest) = old.split_once('.').unwrap();
        let (exp, mac) = rest.split_once('.').unwrap();
        assert_eq!(
            s.verify(&format!("{nonce}.{exp}..{mac}"), "key-a")
                .unwrap_err(),
            Invalid::BadMac
        );
        let new = s.issue("key-a", "");
        let p: Vec<&str> = new.split('.').collect();
        assert_eq!(
            s.verify(&format!("{}.{}.{}", p[0], p[1], p[3]), "key-a")
                .unwrap_err(),
            Invalid::BadMac
        );
    }

    #[test]
    fn every_pod_with_the_secret_agrees_and_others_do_not() {
        let a = Sessions::new(Some("shared"), 60);
        let b = Sessions::new(Some("shared"), 60);
        let other = Sessions::new(Some("different"), 60);
        let id = a.issue("k", "abc");
        assert_eq!(b.verify(&id, "k").unwrap().hash12, "abc");
        assert_eq!(other.verify(&id, "k").unwrap_err(), Invalid::BadMac);
    }

    #[test]
    fn expiry_and_tampering() {
        let s = Sessions::new(Some("x"), 60);
        let expired = s.issue_at("k", "h", now() - 1);
        assert_eq!(s.verify(&expired, "k").unwrap_err(), Invalid::Expired);
        let id = s.issue("k", "h");
        let p: Vec<&str> = id.split('.').collect();
        let (nonce, mac) = (p[0], p[3]);
        // stretching the expiry or swapping the hash with the old MAC is caught as a bad MAC, not as "still valid"
        assert_eq!(
            s.verify(&format!("{nonce}.{}.h.{mac}", now() + 9999), "k")
                .unwrap_err(),
            Invalid::BadMac
        );
        assert_eq!(
            s.verify(&format!("{nonce}.{}.other.{mac}", p[1]), "k")
                .unwrap_err(),
            Invalid::BadMac
        );
        for bad in [
            "",
            "a",
            "a.b",
            "a.b.c.d.e",
            "a.notanumber.c",
            "a.1.!!!",
            "a.1.h.!!!",
        ] {
            assert_eq!(s.verify(bad, "k").unwrap_err(), Invalid::Malformed, "{bad}");
        }
    }

    #[test]
    fn generated_secret_is_flagged_and_per_process() {
        let a = Sessions::new(None, 60);
        let b = Sessions::new(None, 60);
        assert!(a.generated && b.generated);
        assert_eq!(
            b.verify(&a.issue("k", ""), "k").unwrap_err(),
            Invalid::BadMac
        ); // no shared secret → no shared sessions
        assert!(!Sessions::new(Some("s"), 60).generated);
    }

    #[test]
    fn derived_keys_differ_by_label_and_agree_across_pods() {
        let a = Sessions::new(Some("s"), 60);
        let b = Sessions::new(Some("s"), 60);
        let sig = |k: &hmac::Key| hmac::sign(k, b"m").as_ref().to_vec();
        assert_eq!(sig(&a.derived_key("oauth")), sig(&b.derived_key("oauth")));
        assert_ne!(sig(&a.derived_key("oauth")), sig(&a.derived_key("other")));
    }

    #[test]
    fn notified_remembers_once_and_stays_bounded() {
        let n = Notified::new(8);
        assert!(n.first_time("a"));
        assert!(!n.first_time("a"));
        for i in 0..100 {
            n.first_time(&format!("k{i}"));
            assert!(n.len() <= 8, "bounded at cap");
        }
        assert!(n.first_time("a"), "evicted long ago → notified again");
        // a key in the older generation is refreshed, not forgotten
        let n = Notified::new(4);
        assert!(n.first_time("x"));
        assert!(n.first_time("y")); // cur full → next insert rolls generations
        assert!(n.first_time("z")); // x,y → prev; z → cur
        assert!(!n.first_time("x")); // found in prev, moved to cur
        assert!(n.first_time("w")); // cur (z,x) full → rolls: prev = {z,x}, cur = {w}; y is gone
        assert!(n.first_time("y"));
        assert_eq!(Notified::new(0).cap, 2);
    }
}
