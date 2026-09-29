//! Stateless `Mcp-Session-Id`s (CONTRACTS §16.2, D33).
//!
//! `<nonce>.<expiry>.<mac>` with `mac = HMAC-SHA256(secret, nonce ‖ expiry ‖ key_id)`. Any pod holding the zone's
//! secret verifies an id without state, so there is no affinity, no store, and a rollout keeps sessions alive.
//! The MAC binds the id to the credential that minted it: presented under another key it is simply invalid, so a
//! stolen id reveals nothing and cannot be replayed with a different key.
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD as B64;
use ring::{hmac, rand};
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

    /// A fresh id bound to `key_id`, expiring `ttl_secs` from now.
    pub fn issue(&self, key_id: &str) -> String {
        self.issue_at(key_id, now() + self.ttl_secs)
    }

    fn issue_at(&self, key_id: &str, expiry: u64) -> String {
        let nonce = B64.encode(random(NONCE_BYTES));
        let mac = B64.encode(hmac::sign(
            &self.key,
            message(&nonce, expiry, key_id).as_bytes(),
        ));
        format!("{nonce}.{expiry}.{mac}")
    }

    /// Valid for `key_id` and not expired.
    pub fn verify(&self, id: &str, key_id: &str) -> Result<(), Invalid> {
        let mut parts = id.split('.');
        let (Some(nonce), Some(exp), Some(mac), None) =
            (parts.next(), parts.next(), parts.next(), parts.next())
        else {
            return Err(Invalid::Malformed);
        };
        let expiry: u64 = exp.parse().map_err(|_| Invalid::Malformed)?;
        let tag = B64.decode(mac).map_err(|_| Invalid::Malformed)?;
        // MAC first, so an attacker cannot learn whether a forged id is "merely" expired.
        hmac::verify(&self.key, message(nonce, expiry, key_id).as_bytes(), &tag)
            .map_err(|_| Invalid::BadMac)?;
        if expiry <= now() {
            return Err(Invalid::Expired);
        }
        Ok(())
    }

    /// HKDF-style derivation of the OAuth token key from the same secret (§16.3), so one deploy secret covers both.
    pub fn derived_key(&self, label: &str) -> hmac::Key {
        hmac::Key::new(
            hmac::HMAC_SHA256,
            hmac::sign(&self.key, label.as_bytes()).as_ref(),
        )
    }
}

fn message(nonce: &str, expiry: u64, key_id: &str) -> String {
    format!("{nonce}\n{expiry}\n{key_id}")
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
    fn round_trip_binds_the_credential() {
        let s = Sessions::new(Some("zone-secret"), 60);
        let id = s.issue("key-a");
        assert_eq!(s.verify(&id, "key-a"), Ok(()));
        assert_eq!(s.verify(&id, "key-b"), Err(Invalid::BadMac)); // another key cannot reuse it (§16.2)
        assert_eq!(id.split('.').count(), 3);
        assert_ne!(s.issue("key-a"), id); // fresh nonce every time
    }

    #[test]
    fn every_pod_with_the_secret_agrees_and_others_do_not() {
        let a = Sessions::new(Some("shared"), 60);
        let b = Sessions::new(Some("shared"), 60);
        let other = Sessions::new(Some("different"), 60);
        let id = a.issue("k");
        assert_eq!(b.verify(&id, "k"), Ok(()));
        assert_eq!(other.verify(&id, "k"), Err(Invalid::BadMac));
    }

    #[test]
    fn expiry_and_tampering() {
        let s = Sessions::new(Some("x"), 60);
        let expired = s.issue_at("k", now() - 1);
        assert_eq!(s.verify(&expired, "k"), Err(Invalid::Expired));
        let id = s.issue("k");
        let (head, _) = id.rsplit_once('.').unwrap();
        let (nonce, _) = head.split_once('.').unwrap();
        let mac = id.rsplit('.').next().unwrap();
        // stretching the expiry with the old MAC is caught as a bad MAC, not as "still valid"
        assert_eq!(
            s.verify(&format!("{nonce}.{}.{mac}", now() + 9999), "k"),
            Err(Invalid::BadMac)
        );
        for bad in ["", "a", "a.b", "a.b.c.d", "a.notanumber.c", "a.1.!!!"] {
            assert_eq!(s.verify(bad, "k"), Err(Invalid::Malformed), "{bad}");
        }
    }

    #[test]
    fn generated_secret_is_flagged_and_per_process() {
        let a = Sessions::new(None, 60);
        let b = Sessions::new(None, 60);
        assert!(a.generated && b.generated);
        assert_eq!(b.verify(&a.issue("k"), "k"), Err(Invalid::BadMac)); // no shared secret → no shared sessions
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
}
