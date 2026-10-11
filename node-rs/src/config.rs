//! Config precedence: `RAMEN_CONFIG` yaml < process env < bucket deploy file
//! (`<bucket>/.ramen/env-<zone>` or `.ramen/env`, written by the console on deploy; whitelisted keys only,
//! `RAMEN_MCP_KEYS` is the union of all sources). Re-read on `Admin/Reload`.
use ipnet::IpNet;
use std::collections::HashMap;
use std::path::PathBuf;

/// Keys the console may set per deploy. Never bucket/port/python/admin-key/group/zone, and never the
/// proxy-trust (`RAMEN_TRUST_PROXY`, `RAMEN_TRUST_PROXY_HOPS`) or reflection (`RAMEN_REFLECTION`) knobs.
const DEPLOY_KEYS: &[&str] = &[
    "RAMEN_MCP_KEYS",
    "RAMEN_ALLOWED_CIDRS",
    "RAMEN_ENV",
    "RAMEN_VERBOSE",
    "RAMEN_SIDECAR_IDLE_SECS",
    "RAMEN_LOAD_RETRY_SECS",
    "RAMEN_MAX_INFLIGHT",
    "RAMEN_CALL_TIMEOUT_SECS",
    "RAMEN_LOG_FILE",
    "RAMEN_BLOCKED",
    "RAMEN_TOOL_ACCESS",
    "RAMEN_ROLES",
    "RAMEN_ALLOWED_ORIGINS",
    "RAMEN_REDIS_ITEM_URL",
    "RAMEN_REDIS_SCOPE_URL",
    "RAMEN_THROTTLE_ITEM_IP",
    "RAMEN_THROTTLE_ITEM_TOKEN",
    "RAMEN_THROTTLE_SCOPE_IP",
    "RAMEN_THROTTLE_SCOPE_TOKEN",
];

/// C10 (0.7.2): who may see and who may call one tool. Kinds: `key`, `group_admin`, `viewer`, `mcp_user`;
/// `super_admin` never appears here because it is always allowed. A tool absent from the map is unrestricted.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Deserialize)]
pub struct ToolAccess {
    #[serde(default)]
    pub list: Vec<String>,
    #[serde(default)]
    pub call: Vec<String>,
}

/// `RAMEN_TOOL_ACCESS`: compact JSON `{"<tool>": {"list": [...], "call": [...]}}`, written by the console deploy.
pub fn parse_tool_access(text: &str) -> Result<HashMap<String, ToolAccess>, String> {
    serde_json::from_str(text).map_err(|e| {
        format!("RAMEN_TOOL_ACCESS must be a JSON object of tool → {{list, call}}: {e}")
    })
}

/// `RAMEN_ROLES` (§21.4, 0.7.5): compact JSON `{"<custom role>": "<base role>"}` written by the console deploy. A
/// caller whose kind is a custom role matches a `RAMEN_TOOL_ACCESS` list that names the role or its base.
pub fn parse_roles(text: &str) -> Result<HashMap<String, String>, String> {
    let m: HashMap<String, String> = serde_json::from_str(text)
        .map_err(|e| format!("RAMEN_ROLES must be a JSON object of role → base role: {e}"))?;
    if let Some((k, v)) = m
        .iter()
        .find(|(_, v)| !["group_admin", "viewer", "mcp_user"].contains(&v.as_str()))
    {
        return Err(format!(
            "RAMEN_ROLES: {k:?} has base {v:?}; a base is group_admin, viewer or mcp_user"
        ));
    }
    Ok(m)
}

#[derive(Clone, Debug)]
pub struct Config {
    pub port: u16,
    pub bucket: PathBuf,
    /// `gs://bucket/prefix` or `s3://bucket/prefix` synced into `bucket` by the runtime on every load (CONTRACTS §7/§8).
    pub bucket_uri: Option<String>,
    /// `RAMEN_TLS_CERT` + `RAMEN_TLS_KEY` (PEM files): serve TLS (h2) instead of h2c (CONTRACTS §11).
    pub tls: Option<(PathBuf, PathBuf)>,
    pub python: String,
    pub pythonpath: Option<String>,
    pub mcp_keys: Vec<String>,
    pub allowed_cidrs: Vec<IpNet>,
    /// CIDRs allowed to call `Admin/*` (key-protected). Default: any, so IP locks on `Mcp/*` never lock the console out.
    pub admin_cidrs: Vec<IpNet>,
    pub admin_key: Option<String>,
    pub verbose: bool,
    /// Trusted proxy hops in `x-forwarded-for`: the client address is the `trust_proxy_hops`-th entry counted
    /// from the **right** (the end proxies append to). `0` = never read the header, always use the peer address.
    /// Set by `RAMEN_TRUST_PROXY_HOPS`; legacy `RAMEN_TRUST_PROXY=1` means one hop.
    pub trust_proxy_hops: usize,
    /// Register `grpc.reflection.v1[alpha].ServerReflection` (`RAMEN_REFLECTION`, default on). Read once at
    /// startup by `grpc::routes`, so `Admin/Reload` does not change it.
    pub reflection: bool,
    pub group: String,
    pub zone: String,
    pub env: String,
    pub idle_secs: u64,
    /// First retry delay when the initial `runtime.load` fails (doubles up to 60 s; 0 = never retry).
    pub load_retry_secs: u64,
    pub max_inflight: usize,
    pub call_timeout_secs: u64,
    pub log_file: Option<PathBuf>,
    /// Tool/resource/prompt names (or resource URIs) hidden from `*/list` and answered with `-32601` (CONTRACTS §9).
    pub blocked: Vec<String>,
    /// C10: per-tool list/call permissions by caller kind (`RAMEN_TOOL_ACCESS`); empty = every tool unrestricted.
    pub tool_access: HashMap<String, ToolAccess>,
    /// §21.4: custom role → base role (`RAMEN_ROLES`); empty = only the built-in kinds exist.
    pub roles: HashMap<String, String>,
    /// §16.1: browser `Origin` values allowed on `/mcp` (`RAMEN_ALLOWED_ORIGINS`, comma list; `*` = any).
    /// Empty = every request that carries an `Origin` header is refused (DNS-rebinding defence).
    pub allowed_origins: Vec<String>,
    /// §16.2: HMAC key for stateless `Mcp-Session-Id`s and the HKDF root of the OAuth token key
    /// (`RAMEN_SESSION_SECRET`). `None` = generated at startup; sessions then die with the pod.
    pub session_secret: Option<String>,
    /// §16.2: session lifetime (`RAMEN_SESSION_TTL_SECS`, default 1800).
    pub session_ttl_secs: u64,
    /// §16.3: the console URL that issues OAuth tokens (`RAMEN_OAUTH_ISSUER`); tokens are refused without it.
    pub oauth_issuer: Option<String>,
    /// The public base this worker is reached at through the load balancer (`RAMEN_PUBLIC_URL`), for the
    /// absolute `resource_metadata` URL RFC 9728 asks for. Unset → a relative path is advertised.
    pub public_url: Option<String>,
    /// N7: zone-local Redis for the per-tool/resource/prompt throttle (`RAMEN_REDIS_ITEM_URL`); unset disables it.
    pub redis_item_url: Option<String>,
    /// N7: one Redis shared by every zone of this group+env, for the group/environment-level throttle
    /// (`RAMEN_REDIS_SCOPE_URL`); unset disables it.
    pub redis_scope_url: Option<String>,
    /// N7: fixed window per minute, 0 = unlimited. Each pair (ip, token) is checked independently; either
    /// tripping denies the call.
    pub throttle_item_ip: u64,
    pub throttle_item_token: u64,
    pub throttle_scope_ip: u64,
    pub throttle_scope_token: u64,
}

impl Config {
    pub fn load() -> Result<Config, String> {
        Config::from_vars(std::env::vars())
    }

    pub fn from_vars(vars: impl Iterator<Item = (String, String)>) -> Result<Config, String> {
        let env: HashMap<String, String> = vars.filter(|(k, _)| k.starts_with("RAMEN_")).collect();
        let mut map = HashMap::new();
        if let Some(path) = env.get("RAMEN_CONFIG") {
            let text =
                std::fs::read_to_string(path).map_err(|e| format!("RAMEN_CONFIG {path}: {e}"))?;
            map.extend(parse_flat(&text));
        }
        map.extend(env);
        let base = Config::from_map(&map)?;
        let deploy = [
            base.bucket
                .join(".ramen")
                .join(format!("env-{}", base.zone)),
            base.bucket.join(".ramen/env"),
        ];
        let Some(text) = deploy.iter().find_map(|p| std::fs::read_to_string(p).ok()) else {
            return Ok(base);
        };
        for (k, v) in parse_flat(&text) {
            if !DEPLOY_KEYS.contains(&k.as_str()) {
                continue;
            }
            if k == "RAMEN_MCP_KEYS" {
                let merged = map.get(&k).map(|s| format!("{s},{v}")).unwrap_or(v);
                map.insert(k, merged);
            } else {
                map.insert(k, v);
            }
        }
        Config::from_map(&map)
    }

    pub fn from_map(m: &HashMap<String, String>) -> Result<Config, String> {
        let get = |k: &str| {
            m.get(k)
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
        };
        let num = |k: &str, d: u64| -> Result<u64, String> {
            get(k).map_or(Ok(d), |v| {
                v.parse().map_err(|_| format!("{k} must be a number"))
            })
        };
        let list = |k: &str| {
            let mut out: Vec<String> = Vec::new();
            for s in get(k)
                .unwrap_or_default()
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
            {
                if !out.iter().any(|x| x == s) {
                    out.push(s.to_string());
                }
            }
            out
        };
        let allowed_cidrs = if get("RAMEN_ALLOWED_CIDRS").is_some() {
            list("RAMEN_ALLOWED_CIDRS")
        } else {
            vec!["0.0.0.0/0".into(), "::/0".into()]
        }
        .iter()
        .map(|c| {
            c.parse::<IpNet>()
                .map_err(|e| format!("RAMEN_ALLOWED_CIDRS {c}: {e}"))
        })
        .collect::<Result<Vec<_>, _>>()?;
        let admin_cidrs = if get("RAMEN_ADMIN_CIDRS").is_some() {
            list("RAMEN_ADMIN_CIDRS")
        } else {
            vec!["0.0.0.0/0".into(), "::/0".into()]
        }
        .iter()
        .map(|c| {
            c.parse::<IpNet>()
                .map_err(|e| format!("RAMEN_ADMIN_CIDRS {c}: {e}"))
        })
        .collect::<Result<Vec<_>, _>>()?;
        Ok(Config {
            port: num("RAMEN_NODE_PORT", 8080)? as u16,
            bucket: PathBuf::from(get("RAMEN_BUCKET").unwrap_or_else(|| "/buckets/default".into())),
            bucket_uri: get("RAMEN_BUCKET_URI"),
            tls: match (get("RAMEN_TLS_CERT"), get("RAMEN_TLS_KEY")) {
                (Some(c), Some(k)) => Some((PathBuf::from(c), PathBuf::from(k))),
                (None, None) => None,
                _ => return Err("RAMEN_TLS_CERT and RAMEN_TLS_KEY must be set together".into()),
            },
            python: get("RAMEN_PYTHON").unwrap_or_else(|| "python3".into()),
            pythonpath: get("RAMEN_PYTHONPATH"),
            mcp_keys: list("RAMEN_MCP_KEYS"),
            allowed_cidrs,
            admin_cidrs,
            admin_key: get("RAMEN_ADMIN_KEY"),
            verbose: matches!(get("RAMEN_VERBOSE").as_deref(), Some("1" | "true")),
            trust_proxy_hops: match get("RAMEN_TRUST_PROXY_HOPS") {
                Some(_) => num("RAMEN_TRUST_PROXY_HOPS", 0)? as usize,
                // Legacy switch: one hop, i.e. the last (right-most) `x-forwarded-for` entry.
                None => usize::from(matches!(
                    get("RAMEN_TRUST_PROXY").as_deref(),
                    Some("1" | "true")
                )),
            },
            reflection: !matches!(get("RAMEN_REFLECTION").as_deref(), Some("0" | "false")),
            group: get("RAMEN_GROUP").unwrap_or_else(|| "default".into()),
            zone: get("RAMEN_ZONE").unwrap_or_else(|| "local".into()),
            env: get("RAMEN_ENV").unwrap_or_else(|| "default".into()),
            idle_secs: num("RAMEN_SIDECAR_IDLE_SECS", 300)?,
            load_retry_secs: num("RAMEN_LOAD_RETRY_SECS", 5)?,
            max_inflight: num("RAMEN_MAX_INFLIGHT", 32)?.max(1) as usize,
            call_timeout_secs: num("RAMEN_CALL_TIMEOUT_SECS", 120)?,
            log_file: get("RAMEN_LOG_FILE").map(PathBuf::from),
            blocked: list("RAMEN_BLOCKED"),
            tool_access: get("RAMEN_TOOL_ACCESS")
                .map_or(Ok(HashMap::new()), |t| parse_tool_access(&t))?,
            roles: get("RAMEN_ROLES")
                .filter(|t| !t.trim().is_empty())
                .map_or(Ok(HashMap::new()), |t| parse_roles(&t))?,
            allowed_origins: list("RAMEN_ALLOWED_ORIGINS"),
            session_secret: get("RAMEN_SESSION_SECRET").filter(|s| !s.is_empty()),
            session_ttl_secs: num("RAMEN_SESSION_TTL_SECS", 1800)?.max(1),
            oauth_issuer: get("RAMEN_OAUTH_ISSUER")
                .map(|u| u.trim_end_matches('/').to_string())
                .filter(|u| !u.is_empty()),
            public_url: get("RAMEN_PUBLIC_URL")
                .map(|u| u.trim_end_matches('/').to_string())
                .filter(|u| !u.is_empty()),
            redis_item_url: get("RAMEN_REDIS_ITEM_URL"),
            redis_scope_url: get("RAMEN_REDIS_SCOPE_URL"),
            throttle_item_ip: num("RAMEN_THROTTLE_ITEM_IP", 0)?,
            throttle_item_token: num("RAMEN_THROTTLE_ITEM_TOKEN", 0)?,
            throttle_scope_ip: num("RAMEN_THROTTLE_SCOPE_IP", 0)?,
            throttle_scope_token: num("RAMEN_THROTTLE_SCOPE_TOKEN", 0)?,
        })
    }
}

/// Flat `key: value` (yaml) or `KEY=value` (dotenv) lines. Keys are upper-cased and prefixed with
/// `RAMEN_` when missing; `[a, b]` lists become comma lists; quotes are stripped.
pub fn parse_flat(text: &str) -> HashMap<String, String> {
    let mut m = HashMap::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let split = match (line.find(':'), line.find('=')) {
            (Some(c), Some(e)) => Some(if c < e {
                (&line[..c], &line[c + 1..])
            } else {
                (&line[..e], &line[e + 1..])
            }),
            (Some(c), None) => Some((&line[..c], &line[c + 1..])),
            (None, Some(e)) => Some((&line[..e], &line[e + 1..])),
            _ => None,
        };
        let Some((k, v)) = split else { continue };
        let mut key = k.trim().to_ascii_uppercase().replace('-', "_");
        if !key.starts_with("RAMEN_") {
            key = format!("RAMEN_{key}");
        }
        let unq = |s: &str| s.trim().trim_matches(|c| c == '"' || c == '\'').to_string();
        let v = v.trim();
        let v = v
            .strip_prefix('[')
            .and_then(|s| s.strip_suffix(']'))
            .map(|s| s.split(',').map(unq).collect::<Vec<_>>().join(","))
            .unwrap_or_else(|| unq(v));
        m.insert(key, v);
    }
    m
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vars(pairs: &[(&str, &str)]) -> impl Iterator<Item = (String, String)> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect::<Vec<_>>()
            .into_iter()
    }

    #[test]
    fn defaults() {
        let c = Config::from_map(&HashMap::new()).unwrap();
        assert_eq!(
            (c.port, c.idle_secs, c.max_inflight, c.allowed_cidrs.len()),
            (8080, 300, 32, 2)
        );
        assert!(
            c.mcp_keys.is_empty() && c.admin_key.is_none() && !c.verbose && c.log_file.is_none()
        );
        assert!(c.blocked.is_empty());
        let m: HashMap<_, _> = [("RAMEN_BLOCKED".to_string(), " a, b ,a,".to_string())].into();
        assert_eq!(Config::from_map(&m).unwrap().blocked, vec!["a", "b"]);
        assert!(c.bucket_uri.is_none() && c.tls.is_none());
        // proxy trust off and reflection on unless asked otherwise
        assert!(c.trust_proxy_hops == 0 && c.reflection);
        let m: HashMap<_, _> = [("RAMEN_REFLECTION".to_string(), "0".to_string())].into();
        assert!(!Config::from_map(&m).unwrap().reflection);
        let m: HashMap<_, _> = [("RAMEN_REFLECTION".to_string(), "1".to_string())].into();
        assert!(Config::from_map(&m).unwrap().reflection);
        let m: HashMap<_, _> = [("RAMEN_BUCKET_URI".to_string(), " gs://b/g ".to_string())].into();
        assert_eq!(
            Config::from_map(&m).unwrap().bucket_uri.as_deref(),
            Some("gs://b/g")
        );
    }

    #[test]
    fn transport_keys_parse_and_only_origins_is_group_settable() {
        let c = Config::from_map(&HashMap::new()).unwrap();
        assert!(
            c.allowed_origins.is_empty() && c.session_secret.is_none() && c.oauth_issuer.is_none()
        );
        assert_eq!(c.session_ttl_secs, 1800);
        let m: HashMap<_, _> = [
            (
                "RAMEN_ALLOWED_ORIGINS".to_string(),
                "https://a.example, https://b.example".to_string(),
            ),
            ("RAMEN_SESSION_SECRET".to_string(), "s3cret".to_string()),
            ("RAMEN_SESSION_TTL_SECS".to_string(), "0".to_string()),
            (
                "RAMEN_OAUTH_ISSUER".to_string(),
                "https://console.example/".to_string(),
            ),
        ]
        .into();
        let c = Config::from_map(&m).unwrap();
        assert_eq!(
            c.allowed_origins,
            vec!["https://a.example", "https://b.example"]
        );
        assert_eq!(c.session_secret.as_deref(), Some("s3cret"));
        assert_eq!(c.session_ttl_secs, 1); // never zero: an id that expires as it is minted is a bug, not a setting
        assert_eq!(c.oauth_issuer.as_deref(), Some("https://console.example")); // trailing slash normalised
        // a group's deploy file may open its own web origins, but must never rotate the session secret,
        // point the node at another issuer, or stretch session lifetime (§16.2)
        assert!(DEPLOY_KEYS.contains(&"RAMEN_ALLOWED_ORIGINS"));
        for k in [
            "RAMEN_SESSION_SECRET",
            "RAMEN_OAUTH_ISSUER",
            "RAMEN_SESSION_TTL_SECS",
        ] {
            assert!(
                !DEPLOY_KEYS.contains(&k),
                "{k} must not be settable from the bucket"
            );
        }
    }

    /// C10 (0.7.2): `RAMEN_TOOL_ACCESS` is the compact JSON map the console writes into the deploy file.
    #[test]
    fn tool_access_parses_the_json_map_and_is_deploy_scoped() {
        assert!(
            Config::from_map(&HashMap::new())
                .unwrap()
                .tool_access
                .is_empty()
        );
        let m: HashMap<_, _> = [(
            "RAMEN_TOOL_ACCESS".to_string(),
            r#"{"calc":{"list":["key","viewer"],"call":["key"]},"noop":{}}"#.to_string(),
        )]
        .into();
        let c = Config::from_map(&m).unwrap();
        let calc = &c.tool_access["calc"];
        assert_eq!(
            (calc.list.clone(), calc.call.clone()),
            (
                vec!["key".to_string(), "viewer".to_string()],
                vec!["key".to_string()]
            )
        );
        assert!(c.tool_access["noop"].list.is_empty() && c.tool_access["noop"].call.is_empty());
        for bad in [
            "not json",
            "[1]",
            r#"{"calc": 1}"#,
            r#"{"calc": {"list": "key"}}"#,
        ] {
            let m: HashMap<_, _> = [("RAMEN_TOOL_ACCESS".to_string(), bad.to_string())].into();
            assert!(
                Config::from_map(&m)
                    .unwrap_err()
                    .contains("RAMEN_TOOL_ACCESS"),
                "{bad}"
            );
        }
        assert!(DEPLOY_KEYS.contains(&"RAMEN_TOOL_ACCESS"));
    }

    /// §21.4 (0.7.5): `RAMEN_ROLES` maps custom roles to their base; a bad base or shape is a config error.
    #[test]
    fn roles_parse_the_json_map_and_are_deploy_scoped() {
        assert!(Config::from_map(&HashMap::new()).unwrap().roles.is_empty());
        let m: HashMap<_, _> = [("RAMEN_ROLES".to_string(), "".to_string())].into();
        assert!(Config::from_map(&m).unwrap().roles.is_empty());
        let m: HashMap<_, _> = [(
            "RAMEN_ROLES".to_string(),
            r#"{"analyst":"viewer","ops":"group_admin"}"#.to_string(),
        )]
        .into();
        let c = Config::from_map(&m).unwrap();
        assert_eq!(c.roles["analyst"], "viewer");
        assert_eq!(c.roles["ops"], "group_admin");
        for bad in [
            "not json",
            "[1]",
            r#"{"a": 1}"#,
            r#"{"a": "super_admin"}"#,
            r#"{"a": "key"}"#,
        ] {
            let m: HashMap<_, _> = [("RAMEN_ROLES".to_string(), bad.to_string())].into();
            assert!(
                Config::from_map(&m).unwrap_err().contains("RAMEN_ROLES"),
                "{bad}"
            );
        }
        assert!(DEPLOY_KEYS.contains(&"RAMEN_ROLES"));
    }

    #[test]
    fn tls_needs_both_files() {
        let one: HashMap<_, _> = [("RAMEN_TLS_CERT".to_string(), "/c.pem".to_string())].into();
        assert!(
            Config::from_map(&one)
                .unwrap_err()
                .contains("RAMEN_TLS_KEY")
        );
        let both: HashMap<_, _> = [
            ("RAMEN_TLS_CERT".to_string(), "/c.pem".to_string()),
            ("RAMEN_TLS_KEY".to_string(), "/k.pem".to_string()),
        ]
        .into();
        assert_eq!(
            Config::from_map(&both).unwrap().tls,
            Some((PathBuf::from("/c.pem"), PathBuf::from("/k.pem")))
        );
    }

    #[test]
    fn yaml_then_env_wins() {
        let mut m = parse_flat(
            "# c\nnode_port: 9000\nmcp_keys: [a, 'b']\nRAMEN_VERBOSE: \"1\"\nbad line\nDOTENV=x\nurl: http://h:1\n",
        );
        assert_eq!(
            (
                m["RAMEN_MCP_KEYS"].as_str(),
                m["RAMEN_DOTENV"].as_str(),
                m["RAMEN_URL"].as_str()
            ),
            ("a,b", "x", "http://h:1")
        );
        m.insert("RAMEN_NODE_PORT".into(), "9001".into());
        let c = Config::from_map(&m).unwrap();
        assert_eq!(
            (c.port, c.mcp_keys.clone(), c.verbose),
            (9001, vec!["a".to_string(), "b".to_string()], true)
        );
    }

    #[test]
    fn from_vars_reads_yaml_file_then_env_then_deploy_file() {
        let dir = std::env::temp_dir().join(format!("ramen-cfg-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("bucket/.ramen")).unwrap();
        let yaml = dir.join("c.yaml");
        std::fs::write(&yaml, "group: yamlgroup\nzone: z1\nmcp_keys: y1\n").unwrap();
        std::fs::write(
            dir.join("bucket/.ramen/env"),
            "RAMEN_MCP_KEYS=d1,d2\nRAMEN_BUCKET=/evil\nRAMEN_VERBOSE=1\n",
        )
        .unwrap();
        std::fs::write(
            dir.join("bucket/.ramen/env-z1"),
            "RAMEN_MCP_KEYS=rmk_1,e1\nRAMEN_ALLOWED_CIDRS=10.0.0.0/8\nRAMEN_ADMIN_KEY=nope\nRAMEN_BLOCKED=secret_tool\nRAMEN_TOOL_ACCESS={\"calc\":{\"list\":[\"key\"],\"call\":[\"key\"]}}\n",
        )
        .unwrap();
        let bucket = dir.join("bucket").display().to_string();
        let c = Config::from_vars(vars(&[
            ("RAMEN_CONFIG", yaml.to_str().unwrap()),
            ("RAMEN_GROUP", "envgroup"),
            ("RAMEN_BUCKET", &bucket),
            ("RAMEN_MCP_KEYS", "e1"),
            ("HOME", "/x"),
        ]))
        .unwrap();
        assert_eq!((c.group.as_str(), c.zone.as_str()), ("envgroup", "z1"));
        assert_eq!(c.mcp_keys, vec!["e1", "rmk_1"]);
        assert_eq!(c.allowed_cidrs.len(), 1);
        assert!(c.admin_key.is_none() && c.bucket.ends_with("bucket") && !c.verbose);
        assert_eq!(c.blocked, vec!["secret_tool"]);
        assert_eq!(c.tool_access["calc"].call, vec!["key"]); // the deploy file carries the map, like RAMEN_BLOCKED
        let c =
            Config::from_vars(vars(&[("RAMEN_BUCKET", &bucket), ("RAMEN_ZONE", "other")])).unwrap();
        assert_eq!(
            (c.mcp_keys.clone(), c.verbose),
            (vec!["d1".to_string(), "d2".to_string()], true)
        );
        std::fs::remove_dir_all(&dir).unwrap();
        assert!(
            Config::from_vars(vars(&[("RAMEN_CONFIG", "/nonexistent.yaml")]))
                .unwrap_err()
                .contains("RAMEN_CONFIG")
        );
        let _ = Config::load();
    }

    #[test]
    fn bad_values() {
        let m: HashMap<_, _> = [("RAMEN_NODE_PORT".to_string(), "x".to_string())].into();
        assert!(
            Config::from_map(&m)
                .unwrap_err()
                .contains("RAMEN_NODE_PORT")
        );
        let m: HashMap<_, _> =
            [("RAMEN_ALLOWED_CIDRS".to_string(), "10.0.0.0/33".to_string())].into();
        assert!(
            Config::from_map(&m)
                .unwrap_err()
                .contains("RAMEN_ALLOWED_CIDRS")
        );
        let m: HashMap<_, _> = [("RAMEN_MAX_INFLIGHT".to_string(), "0".to_string())].into();
        assert_eq!(Config::from_map(&m).unwrap().max_inflight, 1);
        let m: HashMap<_, _> = [("RAMEN_TRUST_PROXY_HOPS".to_string(), "many".to_string())].into();
        assert!(
            Config::from_map(&m)
                .unwrap_err()
                .contains("RAMEN_TRUST_PROXY_HOPS")
        );
    }
}
