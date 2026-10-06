//! OAuth access tokens the console issues for end users (CONTRACTS §16.3, D34).
//!
//! Compact JWS, HS256, key = HKDF-style derivation of the zone's session secret with the label `oauth`, so the
//! console (which holds the same secret per zone) and every pod of the zone agree without a key exchange. A token
//! is accepted only when its signature, `exp`, `iss`, `aud` and `scope` all check out; anything else is refused with
//! the reason, and the reason never says which check failed to the caller — only the access log learns it.
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD as B64;
use ring::hmac;
use serde_json::Value;

/// What a valid token establishes about the caller.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Principal {
    /// `sub`: the console user id; this is what the access log records as the consumer.
    pub sub: String,
    pub email: Option<String>,
    pub jti: Option<String>,
    /// C10 (0.7.2): the person's role in the group of the scope (`group_admin`, `viewer`, `mcp_user`, or
    /// `super_admin`), as the console minted it. Absent on older tokens → the node treats them as `mcp_user`.
    pub role: Option<String>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Reject {
    Malformed,
    BadSignature,
    Expired,
    WrongIssuer,
    WrongAudience,
    WrongScope,
}

/// The audience and scope a worker for `group`/`zone` accepts. The same string is what the worker publishes as its
/// protected-resource identifier, so the console mints tokens for exactly this.
pub fn resource(group: &str, zone: &str) -> String {
    format!("mcp:{group}:{zone}")
}

/// Verify `token` for this worker. `issuer` is the console URL (`RAMEN_OAUTH_ISSUER`); `now` is unix seconds.
pub fn verify(
    token: &str,
    key: &hmac::Key,
    issuer: &str,
    group: &str,
    zone: &str,
    now: u64,
) -> Result<Principal, Reject> {
    let mut parts = token.split('.');
    let (Some(h), Some(p), Some(s), None) =
        (parts.next(), parts.next(), parts.next(), parts.next())
    else {
        return Err(Reject::Malformed);
    };
    let header: Value = serde_json::from_slice(&B64.decode(h).map_err(|_| Reject::Malformed)?)
        .map_err(|_| Reject::Malformed)?;
    if header.get("alg").and_then(Value::as_str) != Some("HS256") {
        return Err(Reject::Malformed); // `none` and every other algorithm are refused before any parsing of claims
    }
    let sig = B64.decode(s).map_err(|_| Reject::Malformed)?;
    hmac::verify(key, format!("{h}.{p}").as_bytes(), &sig).map_err(|_| Reject::BadSignature)?;
    let claims: Value = serde_json::from_slice(&B64.decode(p).map_err(|_| Reject::Malformed)?)
        .map_err(|_| Reject::Malformed)?;
    let exp = claims
        .get("exp")
        .and_then(Value::as_u64)
        .ok_or(Reject::Malformed)?;
    if exp <= now {
        return Err(Reject::Expired);
    }
    // Either side may carry a trailing slash (a console URL is often typed with one); neither may differ otherwise.
    if claims
        .get("iss")
        .and_then(Value::as_str)
        .map(|i| i.trim_end_matches('/'))
        != Some(issuer.trim_end_matches('/'))
    {
        return Err(Reject::WrongIssuer);
    }
    let want = resource(group, zone);
    let aud_ok = match claims.get("aud") {
        Some(Value::String(a)) => a == &want,
        Some(Value::Array(v)) => v.iter().any(|a| a.as_str() == Some(want.as_str())),
        _ => false,
    };
    if !aud_ok {
        return Err(Reject::WrongAudience);
    }
    let scope_ok = claims
        .get("scope")
        .and_then(Value::as_str)
        .is_some_and(|sc| sc.split_whitespace().any(|s| s == want));
    if !scope_ok {
        return Err(Reject::WrongScope);
    }
    let sub = claims
        .get("sub")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or(Reject::Malformed)?;
    Ok(Principal {
        sub: sub.to_string(),
        email: claims
            .get("email")
            .and_then(Value::as_str)
            .map(String::from),
        jti: claims.get("jti").and_then(Value::as_str).map(String::from),
        role: claims
            .get("role")
            .and_then(Value::as_str)
            .filter(|r| !r.is_empty())
            .map(String::from),
    })
}

/// Mint a token with the same primitives — used by the tests here and by the harness; the console has its own
/// implementation in Python and the conformance suite proves the two agree.
pub fn mint(claims: &Value, key: &hmac::Key) -> String {
    let h = B64.encode(br#"{"alg":"HS256","typ":"JWT"}"#);
    let p = B64.encode(claims.to_string());
    let sig = B64.encode(hmac::sign(key, format!("{h}.{p}").as_bytes()));
    format!("{h}.{p}.{sig}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn key(secret: &str) -> hmac::Key {
        crate::session::Sessions::new(Some(secret), 60).derived_key("oauth")
    }

    fn claims() -> Value {
        json!({"iss": "https://console.example", "sub": "u1", "email": "ada@example.com", "aud": "mcp:demo:a",
               "scope": "mcp:demo:a", "exp": 2_000_000_000u64, "jti": "j1"})
    }

    #[test]
    fn a_console_token_for_this_worker_is_accepted() {
        let t = mint(&claims(), &key("s"));
        let p = verify(
            &t,
            &key("s"),
            "https://console.example/",
            "demo",
            "a",
            1_900_000_000,
        )
        .unwrap();
        assert_eq!(p.sub, "u1");
        assert_eq!(p.email.as_deref(), Some("ada@example.com"));
        assert_eq!(p.jti.as_deref(), Some("j1"));
        assert_eq!(p.role, None); // C10: no claim → the node treats the caller as `mcp_user`
        let mut c = claims();
        c["role"] = json!("viewer");
        let p = verify(
            &mint(&c, &key("s")),
            &key("s"),
            "https://console.example",
            "demo",
            "a",
            1_900_000_000,
        )
        .unwrap();
        assert_eq!(p.role.as_deref(), Some("viewer"));
    }

    #[test]
    fn every_check_refuses_on_its_own() {
        let k = key("s");
        let ok = |c: Value| {
            verify(
                &mint(&c, &k),
                &k,
                "https://console.example",
                "demo",
                "a",
                1_900_000_000,
            )
        };
        assert_eq!(
            verify(
                &mint(&claims(), &key("other")),
                &k,
                "https://console.example",
                "demo",
                "a",
                1
            )
            .unwrap_err(),
            Reject::BadSignature
        );
        assert_eq!(
            verify(
                &mint(&claims(), &k),
                &k,
                "https://console.example",
                "demo",
                "a",
                2_000_000_001
            )
            .unwrap_err(),
            Reject::Expired
        );
        let mut c = claims();
        c["iss"] = json!("https://evil.example");
        assert_eq!(ok(c).unwrap_err(), Reject::WrongIssuer);
        let mut c = claims();
        c["aud"] = json!("mcp:demo:b"); // another zone's token
        assert_eq!(ok(c).unwrap_err(), Reject::WrongAudience);
        let mut c = claims();
        c["aud"] = json!(["mcp:other:a", "mcp:demo:a"]); // audience lists are fine when one entry is us
        assert!(ok(c).is_ok());
        let mut c = claims();
        c["scope"] = json!("mcp:other:a");
        assert_eq!(ok(c).unwrap_err(), Reject::WrongScope);
        let mut c = claims();
        c["sub"] = json!("");
        assert_eq!(ok(c).unwrap_err(), Reject::Malformed);
        let mut c = claims();
        c.as_object_mut().unwrap().remove("exp");
        assert_eq!(ok(c).unwrap_err(), Reject::Malformed);
    }

    #[test]
    fn algorithm_confusion_is_refused_before_claims_are_read() {
        let k = key("s");
        let h = B64.encode(br#"{"alg":"none"}"#);
        let p = B64.encode(claims().to_string());
        assert_eq!(
            verify(
                &format!("{h}.{p}."),
                &k,
                "https://console.example",
                "demo",
                "a",
                1
            )
            .unwrap_err(),
            Reject::Malformed
        );
        for bad in ["", "a", "a.b", "a.b.c.d", "!!.!!.!!"] {
            assert_eq!(
                verify(bad, &k, "https://console.example", "demo", "a", 1).unwrap_err(),
                Reject::Malformed,
                "{bad}"
            );
        }
    }
}
