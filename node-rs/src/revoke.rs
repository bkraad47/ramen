//! D92 (CONTRACTS §16.7): revocation that takes effect at once. `RAMEN_REVOKED` is the console's compact JSON
//! `{"keys": [<sha256 hex of an rmk_ key>], "jti": [<token id>], "sub": {<user id>: <unix secs>}}`: a key on the
//! list is dropped from `RAMEN_MCP_KEYS`, a token whose `jti` is listed — or whose `sub` is listed with an `iat` at or
//! before that time — is refused. Every source (env, deploy file, the overlay an `Admin/Reload` carries) is merged,
//! never replaced: a revocation only ever adds, and an entry outlives no token it covers (the console prunes them).
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Deserialize)]
pub struct Revoked {
    #[serde(default)]
    pub keys: BTreeSet<String>,
    #[serde(default)]
    pub jti: BTreeSet<String>,
    #[serde(default)]
    pub sub: BTreeMap<String, u64>,
}

/// The list's id of a key: lowercase hex SHA-256 of the raw key (the console's `revocation.key_hash`).
pub fn key_hash(key: &str) -> String {
    ring::digest::digest(&ring::digest::SHA256, key.as_bytes())
        .as_ref()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

impl Revoked {
    pub fn parse(text: &str) -> Result<Revoked, String> {
        if text.trim().is_empty() {
            return Ok(Revoked::default());
        }
        serde_json::from_str(text)
            .map_err(|e| format!("RAMEN_REVOKED must be a JSON object of keys, jti and sub: {e}"))
    }

    pub fn is_empty(&self) -> bool {
        self.keys.is_empty() && self.jti.is_empty() && self.sub.is_empty()
    }

    /// Union: keys and token ids add up, a subject keeps its latest cut-off.
    pub fn merge(&mut self, other: &Revoked) {
        self.keys.extend(other.keys.iter().cloned());
        self.jti.extend(other.jti.iter().cloned());
        for (s, t) in &other.sub {
            let e = self.sub.entry(s.clone()).or_insert(*t);
            *e = (*e).max(*t);
        }
    }

    pub fn key_revoked(&self, key: &str) -> bool {
        !self.keys.is_empty() && self.keys.contains(&key_hash(key))
    }

    /// A token is refused when its id is listed, or its subject is listed and it was issued at or before the cut-off
    /// (a token without `iat` is then refused too: nothing proves it is newer).
    pub fn token_revoked(&self, sub: &str, jti: Option<&str>, iat: Option<u64>) -> bool {
        if jti.is_some_and(|j| self.jti.contains(j)) {
            return true;
        }
        self.sub
            .get(sub)
            .is_some_and(|before| iat.is_none_or(|i| i <= *before))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_merges_and_answers() {
        let mut r = Revoked::parse(&format!(
            r#"{{"keys":["{}"],"jti":["j1"],"sub":{{"u1":100}}}}"#,
            key_hash("rmk_gone")
        ))
        .unwrap();
        assert!(r.key_revoked("rmk_gone") && !r.key_revoked("rmk_live"));
        assert!(r.token_revoked("u9", Some("j1"), Some(500)));
        assert!(r.token_revoked("u1", Some("j2"), Some(100))); // issued at the cut-off: refused
        assert!(r.token_revoked("u1", None, None)); // no iat: nothing proves it is newer
        assert!(!r.token_revoked("u1", Some("j2"), Some(101))); // signed in again after it: fine
        assert!(!r.token_revoked("u2", Some("j3"), Some(1)));
        r.merge(&Revoked::parse(r#"{"jti":["j4"],"sub":{"u1":50,"u2":7}}"#).unwrap());
        assert_eq!(r.sub["u1"], 100); // the later cut-off stays
        assert!(r.token_revoked("u2", None, Some(7)) && r.jti.contains("j4"));
        assert!(Revoked::parse("").unwrap().is_empty() && Revoked::parse("{}").unwrap().is_empty());
        assert!(Revoked::parse("[1]").is_err() && Revoked::parse(r#"{"sub":{"u":"x"}}"#).is_err());
        assert_eq!(
            key_hash("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn every_source_adds_and_a_listed_key_is_gone() {
        use crate::config::Config;
        let dir = std::env::temp_dir().join(format!("ramen-revoke-{}", std::process::id()));
        std::fs::create_dir_all(dir.join(".ramen")).unwrap();
        // the env (the deploy Secret on a cloud) still carries the key; the deploy file revokes it
        std::fs::write(
            dir.join(".ramen/env"),
            format!(
                "RAMEN_MCP_KEYS=rmk_new\nRAMEN_REVOKED={{\"keys\":[\"{}\"],\"jti\":[\"j2\"]}}\n",
                key_hash("rmk_old")
            ),
        )
        .unwrap();
        let vars = [
            ("RAMEN_BUCKET", dir.to_str().unwrap()),
            ("RAMEN_MCP_KEYS", "rmk_old,rmk_keep"),
            ("RAMEN_REVOKED", r#"{"jti":["j1"],"sub":{"u1":9}}"#),
        ];
        let mut c =
            Config::from_vars(vars.iter().map(|(k, v)| (k.to_string(), v.to_string()))).unwrap();
        assert_eq!(c.mcp_keys, vec!["rmk_keep", "rmk_new"]);
        assert_eq!(c.revoked.jti.len(), 2);
        assert_eq!(c.revoked.sub["u1"], 9);
        c.revoke(&Revoked::parse(&format!(r#"{{"keys":["{}"]}}"#, key_hash("rmk_new"))).unwrap());
        assert_eq!(c.mcp_keys, vec!["rmk_keep"]);
        std::fs::write(dir.join(".ramen/env"), "RAMEN_REVOKED=not json\n").unwrap();
        assert!(
            Config::from_vars(vars.iter().map(|(k, v)| (k.to_string(), v.to_string()))).is_err()
        );
        std::fs::remove_dir_all(&dir).ok();
        let one = [("RAMEN_MCP_KEYS", "a,b"), ("RAMEN_REVOKED", "{")];
        assert!(
            Config::from_vars(one.iter().map(|(k, v)| (k.to_string(), v.to_string()))).is_err()
        );
    }
}
