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
    // §23 (1.0.0): tunables of the 2026-07-28 surface. `RAMEN_MRTR_TTL_SECS` is not here for the reason
    // `RAMEN_SESSION_TTL_SECS` is not: a group must not stretch the life of a signed token.
    "RAMEN_LIST_TTL_MS",
    "RAMEN_TASK_TTL_SECS",
    "RAMEN_MAX_TASKS",
    "RAMEN_MAX_SUBSCRIPTIONS",
    // §24.3 (1.0.0): where the node exports, whether callers must consent, and what `server/discover` tells them.
    // The activator keys (`RAMEN_ACTIVATOR*`, `RAMEN_CONSOLE_INTERNAL_URL`, `RAMEN_WAKE_TIMEOUT_SECS`) are the
    // deployment's, never a group's.
    "RAMEN_OTEL_ENDPOINT",
    "RAMEN_OTEL_HEADERS",
    "RAMEN_OTEL_CLIENT",
    "RAMEN_TELEMETRY_CONSENT",
    "RAMEN_TELEMETRY_NOTICE",
    "RAMEN_TELEMETRY_VERSION",
    // §29.3 (1.0.0c): the evals config version a call must name, and whether the group requires it.
    "RAMEN_EVALS_VERSION",
    "RAMEN_EVALS_REQUIRED",
    // §30.3 (1.0.0c): the skillspector policy version a call must name, and whether the group requires it.
    "RAMEN_SKILLSPECTOR_VERSION",
    "RAMEN_SKILLSPECTOR_REQUIRED",
    // §31.2 (1.0.0c): the LLM configs version a call must name, and whether the repo carries the directory.
    "RAMEN_CONFIGS_VERSION",
    "RAMEN_CONFIGS_REQUIRED",
    // §36.3 (1.0.0c): the guardrails policy version a call must name, and whether the repo carries the rails.
    "RAMEN_GUARDRAILS_VERSION",
    "RAMEN_GUARDRAILS_REQUIRED",
    // §36.2 (1.0.0c): the LLM the group's rails call — the deploy writes it, the runtime child reads it (never the
    // node; `RAMEN_LLM_API_KEY` is never logged)
    "RAMEN_LLM_PROVIDER",
    "RAMEN_LLM_MODEL",
    "RAMEN_LLM_API_KEY",
    "RAMEN_LLM_BASE_URL",
    "RAMEN_LLM_ENDPOINT",
    "RAMEN_LLM_DEPLOYMENT",
    "RAMEN_LLM_API_VERSION",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "AWS_REGION",
    // §31.4a (D76): the per-deploy key Ramen's own evals Job calls with; MCP methods only, no policy gate.
    "RAMEN_EVALS_KEY",
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
    /// `RAMEN_TLS_CERT` + `RAMEN_TLS_KEY` (PEM files): serve TLS (h2) instead of h2c (CONTRACTS §11); re-read when
    /// they change (§39.2).
    pub tls: Option<(PathBuf, PathBuf)>,
    /// §39.4 (D86): `RAMEN_REQUIRE_TLS=1` — no start without the pair, no plaintext URL. Startup-fixed.
    pub require_tls: bool,
    /// §39.2: `RAMEN_TLS_CA`, a PEM bundle trusted besides the public roots by the node's own calls (wake, OTLP,
    /// the activator's upstream, in-cluster Redis).
    pub tls_ca: Option<PathBuf>,
    /// §39.2: the name an in-install TLS peer is verified as (`RAMEN_TLS_SERVER_NAME`, default
    /// `worker.ramen.internal`): the activator's upstream.
    pub tls_server_name: String,
    /// §39.4: plaintext URLs dropped under `require_tls` (the keys, never the values): fatal at start, a warning at
    /// `Admin/Reload` with that feature off.
    pub plaintext_refused: Vec<String>,
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
    /// §39.5a: the bound on every Redis operation a call waits for — throttle `INCR`, task and consent store — and on
    /// each connection attempt (`RAMEN_REDIS_TIMEOUT_MS`, default 500, 50–10000). Past it the throttle fails open and
    /// the stores answer an error; the connection is re-dialed in the background. Startup-fixed.
    pub redis_timeout: std::time::Duration,
    /// N7: fixed window per minute, 0 = unlimited. Each pair (ip, token) is checked independently; either
    /// tripping denies the call.
    pub throttle_item_ip: u64,
    pub throttle_item_token: u64,
    pub throttle_scope_ip: u64,
    pub throttle_scope_token: u64,
    /// §23.1: `ttlMs` on modern list / discover results (`RAMEN_LIST_TTL_MS`, default 60000).
    pub list_ttl_ms: u64,
    /// §23.4: lifetime of a signed `requestState` (`RAMEN_MRTR_TTL_SECS`, default 600).
    pub mrtr_ttl_secs: u64,
    /// §23.5: a task's `ttlMs` / 1000 (`RAMEN_TASK_TTL_SECS`, default 3600).
    pub task_ttl_secs: u64,
    /// §23.5: tasks running on this pod at once (`RAMEN_MAX_TASKS`, default 4); beyond it a task call is 429.
    pub max_tasks: usize,
    /// §23.6: open `subscriptions/listen` streams per pod (`RAMEN_MAX_SUBSCRIPTIONS`, default 64), then 429.
    pub max_subscriptions: usize,
    /// §24.1: this process is a zone's activator (`RAMEN_ACTIVATOR=1`): guards, wake, forward — never a runtime.
    /// Startup-fixed.
    pub activator: bool,
    /// §24.1: the console's in-cluster base URL (`RAMEN_CONSOLE_INTERNAL_URL`) the activator posts the wake to.
    pub console_internal_url: Option<String>,
    /// §24.1: where the zone's real workers answer (`RAMEN_ACTIVATOR_UPSTREAM`, default `worker-direct:8080`;
    /// `host:port`, `http://host:port` or `https://host:port`; a bare `host:port` is TLS when the node has a pair or
    /// `RAMEN_TLS_CA` — §39.3).
    pub activator_upstream: String,
    /// §24.1: how long a call waits for the zone to wake (`RAMEN_WAKE_TIMEOUT_SECS`, default 25: under the
    /// default backend timeout of every edge — GKE 30 s, ALB 60 s — so the caller gets our 503, not the edge's 504).
    pub wake_timeout_secs: u64,
    /// §24.3: OTLP/HTTP base URL (`RAMEN_OTEL_ENDPOINT`); unset = nothing is exported.
    pub otel_endpoint: Option<String>,
    /// §24.3: headers sent to the collector (`RAMEN_OTEL_HEADERS`, a JSON object of strings). Secret.
    pub otel_headers: Vec<(String, String)>,
    /// §24.3: what `server/discover` hands a caller under `_meta["io.ramen/telemetry"]` (`RAMEN_OTEL_CLIENT`, a
    /// JSON object written by the deploy); unset = no entry.
    pub otel_client: Option<serde_json::Value>,
    /// §24.3: `tools/call`, `prompts/get` and `resources/read` need the caller's consent (`RAMEN_TELEMETRY_CONSENT`).
    pub telemetry_consent: bool,
    /// §24.3: where the telemetry goes, as the consent prompt names it (`RAMEN_TELEMETRY_NOTICE`).
    pub telemetry_notice: String,
    /// §26.3: the console's telemetry config version a report must name (`RAMEN_TELEMETRY_VERSION`, else the
    /// `version` key of `RAMEN_OTEL_CLIENT`); empty = not told yet, any version is taken.
    pub telemetry_version: String,
    /// §29.3: the group's evals config version a gated call must name (`RAMEN_EVALS_VERSION`); empty = not told
    /// yet, any version is taken.
    pub evals_version: String,
    /// §29.3: whether gated calls must name the evals version at all (`RAMEN_EVALS_REQUIRED`).
    pub evals_required: bool,
    /// §30.3: the group's skillspector policy version a gated call must name (`RAMEN_SKILLSPECTOR_VERSION`);
    /// empty = not told yet, any version is taken.
    pub skillspector_version: String,
    /// §30.3: whether gated calls must name the skillspector version at all (`RAMEN_SKILLSPECTOR_REQUIRED`).
    pub skillspector_required: bool,
    /// §31.2: the group's LLM configs version a gated call must name (`RAMEN_CONFIGS_VERSION`); empty = not told
    /// yet, any version is taken.
    pub configs_version: String,
    /// §31.2: whether gated calls must name the configs version at all (`RAMEN_CONFIGS_REQUIRED`).
    pub configs_required: bool,
    /// §36.3: the group's guardrails policy version a gated call must name (`RAMEN_GUARDRAILS_VERSION`); empty =
    /// not told yet, any version is taken.
    pub guardrails_version: String,
    /// §36.3: whether gated calls must name the guardrails version at all (`RAMEN_GUARDRAILS_REQUIRED`).
    pub guardrails_required: bool,
    /// §31.4a: the bearer of Ramen's own evals Job (`RAMEN_EVALS_KEY`, rotated per deploy); none = nothing accepted.
    pub evals_key: Option<String>,
    /// §36.2: what the runtime child gets for its LLM rails (`RUNTIME_KEYS` present in the env or the deploy file).
    pub runtime_env: RuntimeEnv,
}

/// §36.2: the keys passed on to the runtime child (the deploy file is read by the node alone, the child only inherits
/// the node's process env — `sidecar.rs`); the value of `RAMEN_LLM_API_KEY` never appears in a log line or a `Debug`.
pub const RUNTIME_KEYS: &[&str] = &[
    "RAMEN_LLM_PROVIDER",
    "RAMEN_LLM_MODEL",
    "RAMEN_LLM_API_KEY",
    "RAMEN_LLM_BASE_URL",
    "RAMEN_LLM_ENDPOINT",
    "RAMEN_LLM_DEPLOYMENT",
    "RAMEN_LLM_API_VERSION",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "AWS_REGION",
];

#[derive(Clone, Default, PartialEq, Eq)]
pub struct RuntimeEnv(pub std::collections::BTreeMap<String, String>);

impl RuntimeEnv {
    fn from_map(m: &HashMap<String, String>) -> Self {
        RuntimeEnv(
            RUNTIME_KEYS
                .iter()
                .filter_map(|k| {
                    m.get(*k)
                        .map(|v| v.trim())
                        .filter(|v| !v.is_empty())
                        .map(|v| (k.to_string(), v.to_string()))
                })
                .collect(),
        )
    }

    pub fn iter(&self) -> impl Iterator<Item = (&str, &str)> {
        self.0.iter().map(|(k, v)| (k.as_str(), v.as_str()))
    }
}

impl std::fmt::Debug for RuntimeEnv {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let mut m = f.debug_map();
        for (k, v) in &self.0 {
            if k.contains("KEY") {
                m.entry(k, &"***");
            } else {
                m.entry(k, v);
            }
        }
        m.finish()
    }
}

/// `RAMEN_OTEL_HEADERS`: a JSON object of header name → string value.
pub fn parse_otel_headers(text: &str) -> Result<Vec<(String, String)>, String> {
    let bad = || "RAMEN_OTEL_HEADERS must be a JSON object of header → string".to_string();
    let m: std::collections::BTreeMap<String, String> =
        serde_json::from_str(text).map_err(|_| bad())?; // the parse error could quote a header value: not echoed
    if m.iter().any(|(k, v)| {
        http::HeaderName::from_bytes(k.as_bytes()).is_err()
            || http::HeaderValue::from_str(v).is_err()
    }) {
        return Err(bad());
    }
    Ok(m.into_iter().collect())
}

impl Config {
    /// §39.4: what stops the node at start beyond a parse error — a plaintext URL dropped under `RAMEN_REQUIRE_TLS`.
    pub fn startup_error(&self) -> Option<String> {
        (!self.plaintext_refused.is_empty()).then(|| {
            format!(
                "RAMEN_REQUIRE_TLS=1: plaintext URL refused in {} (D86: use https:// / rediss://)",
                self.plaintext_refused.join(", ")
            )
        })
    }

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
        // §39.4 (D86): plaintext refused. The deployment's own keys are fatal; the deploy file's (collector, Redis)
        // can change at a reload, so they are dropped and named.
        let require_tls = matches!(get("RAMEN_REQUIRE_TLS").as_deref(), Some("1" | "true"));
        let mut plaintext_refused = Vec::new();
        let mut secure = |k: &str, insecure: &str| -> Option<String> {
            let v = get(k)?;
            if require_tls && v.to_ascii_lowercase().starts_with(insecure) {
                plaintext_refused.push(k.to_string());
                return None;
            }
            Some(v)
        };
        let otel_endpoint = secure("RAMEN_OTEL_ENDPOINT", "http://");
        let redis_item_url = secure("RAMEN_REDIS_ITEM_URL", "redis://");
        let redis_scope_url = secure("RAMEN_REDIS_SCOPE_URL", "redis://");
        if require_tls {
            if get("RAMEN_TLS_CERT").is_none() || get("RAMEN_TLS_KEY").is_none() {
                return Err(
                    "RAMEN_REQUIRE_TLS=1: RAMEN_TLS_CERT and RAMEN_TLS_KEY are required (D86: plaintext is refused)"
                        .into(),
                );
            }
            for k in ["RAMEN_CONSOLE_INTERNAL_URL", "RAMEN_ACTIVATOR_UPSTREAM"] {
                if get(k).is_some_and(|v| v.to_ascii_lowercase().starts_with("http://")) {
                    return Err(format!(
                        "RAMEN_REQUIRE_TLS=1: {k} must not be http:// (D86: plaintext is refused)"
                    ));
                }
            }
        }
        Ok(Config {
            port: num("RAMEN_NODE_PORT", 8080)? as u16,
            bucket: PathBuf::from(get("RAMEN_BUCKET").unwrap_or_else(|| "/buckets/default".into())),
            bucket_uri: get("RAMEN_BUCKET_URI"),
            tls: match (get("RAMEN_TLS_CERT"), get("RAMEN_TLS_KEY")) {
                (Some(c), Some(k)) => Some((PathBuf::from(c), PathBuf::from(k))),
                (None, None) => None,
                _ => return Err("RAMEN_TLS_CERT and RAMEN_TLS_KEY must be set together".into()),
            },
            require_tls,
            tls_ca: get("RAMEN_TLS_CA").map(PathBuf::from),
            tls_server_name: get("RAMEN_TLS_SERVER_NAME")
                .unwrap_or_else(|| "worker.ramen.internal".into()),
            plaintext_refused,
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
            redis_item_url,
            redis_scope_url,
            redis_timeout: std::time::Duration::from_millis(
                num("RAMEN_REDIS_TIMEOUT_MS", 500)?.clamp(50, 10_000),
            ),
            throttle_item_ip: num("RAMEN_THROTTLE_ITEM_IP", 0)?,
            throttle_item_token: num("RAMEN_THROTTLE_ITEM_TOKEN", 0)?,
            throttle_scope_ip: num("RAMEN_THROTTLE_SCOPE_IP", 0)?,
            throttle_scope_token: num("RAMEN_THROTTLE_SCOPE_TOKEN", 0)?,
            list_ttl_ms: num("RAMEN_LIST_TTL_MS", 60_000)?,
            mrtr_ttl_secs: num("RAMEN_MRTR_TTL_SECS", 600)?.max(1),
            task_ttl_secs: num("RAMEN_TASK_TTL_SECS", 3600)?.max(1),
            max_tasks: num("RAMEN_MAX_TASKS", 4)? as usize,
            max_subscriptions: num("RAMEN_MAX_SUBSCRIPTIONS", 64)? as usize,
            activator: matches!(get("RAMEN_ACTIVATOR").as_deref(), Some("1" | "true")),
            console_internal_url: get("RAMEN_CONSOLE_INTERNAL_URL")
                .map(|u| u.trim_end_matches('/').to_string())
                .filter(|u| !u.is_empty()),
            activator_upstream: get("RAMEN_ACTIVATOR_UPSTREAM")
                .map(|u| u.trim_end_matches('/').to_string())
                .unwrap_or_else(|| "worker-direct:8080".into()),
            wake_timeout_secs: num("RAMEN_WAKE_TIMEOUT_SECS", 25)?.max(1),
            otel_endpoint: otel_endpoint
                .map(|u| u.trim_end_matches('/').to_string())
                .filter(|u| !u.is_empty()),
            otel_headers: get("RAMEN_OTEL_HEADERS")
                .map_or(Ok(Vec::new()), |t| parse_otel_headers(&t))?,
            otel_client: match get("RAMEN_OTEL_CLIENT") {
                None => None,
                Some(t) => Some(
                    serde_json::from_str::<serde_json::Value>(&t)
                        .ok()
                        .filter(serde_json::Value::is_object)
                        .ok_or("RAMEN_OTEL_CLIENT must be a JSON object")?,
                ),
            },
            telemetry_consent: matches!(
                get("RAMEN_TELEMETRY_CONSENT").as_deref(),
                Some("1" | "true")
            ),
            telemetry_notice: get("RAMEN_TELEMETRY_NOTICE").unwrap_or_default(),
            telemetry_version: get("RAMEN_TELEMETRY_VERSION")
                .or_else(|| {
                    get("RAMEN_OTEL_CLIENT")
                        .and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok())
                        .and_then(|v| v["version"].as_str().map(String::from))
                })
                .unwrap_or_default(),
            evals_version: get("RAMEN_EVALS_VERSION").unwrap_or_default(),
            evals_required: matches!(get("RAMEN_EVALS_REQUIRED").as_deref(), Some("1" | "true")),
            skillspector_version: get("RAMEN_SKILLSPECTOR_VERSION").unwrap_or_default(),
            skillspector_required: matches!(
                get("RAMEN_SKILLSPECTOR_REQUIRED").as_deref(),
                Some("1" | "true")
            ),
            configs_version: get("RAMEN_CONFIGS_VERSION").unwrap_or_default(),
            configs_required: matches!(
                get("RAMEN_CONFIGS_REQUIRED").as_deref(),
                Some("1" | "true")
            ),
            guardrails_version: get("RAMEN_GUARDRAILS_VERSION").unwrap_or_default(),
            guardrails_required: matches!(
                get("RAMEN_GUARDRAILS_REQUIRED").as_deref(),
                Some("1" | "true")
            ),
            evals_key: get("RAMEN_EVALS_KEY"),
            runtime_env: RuntimeEnv::from_map(m),
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
        // §36.2: the cloud keys the runtime child reads keep their own names; everything else is Ramen's
        if !key.starts_with("RAMEN_") && !RUNTIME_KEYS.contains(&key.as_str()) {
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

    /// §39.4 (D86): under `RAMEN_REQUIRE_TLS=1` the node never starts without its pair and never dials in the clear;
    /// what a later `Admin/Reload` can change (collector, Redis) is dropped and named instead of failing the reload.
    #[test]
    fn require_tls_refuses_plaintext() {
        let m = |pairs: &[(&str, &str)]| -> HashMap<String, String> {
            pairs
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect()
        };
        let pair = [
            ("RAMEN_REQUIRE_TLS", "1"),
            ("RAMEN_TLS_CERT", "/c.pem"),
            ("RAMEN_TLS_KEY", "/k.pem"),
        ];
        let e = Config::from_map(&m(&[("RAMEN_REQUIRE_TLS", "1")])).unwrap_err();
        assert!(
            e.contains("RAMEN_REQUIRE_TLS") && e.contains("RAMEN_TLS_CERT"),
            "{e}"
        );
        let ok = Config::from_map(&m(&pair)).unwrap();
        assert!(ok.require_tls && ok.plaintext_refused.is_empty());
        assert_eq!(ok.tls_server_name, "worker.ramen.internal");
        let mut with = pair.to_vec();
        with.push((
            "RAMEN_CONSOLE_INTERNAL_URL",
            "http://console.ramen-system.svc.cluster.local:80",
        ));
        let e = Config::from_map(&m(&with)).unwrap_err();
        assert!(e.contains("RAMEN_CONSOLE_INTERNAL_URL"), "{e}");
        let mut with = pair.to_vec();
        with.push(("RAMEN_ACTIVATOR_UPSTREAM", "http://worker-direct:8080"));
        assert!(
            Config::from_map(&m(&with))
                .unwrap_err()
                .contains("RAMEN_ACTIVATOR_UPSTREAM")
        );
        let mut with = pair.to_vec();
        with.extend([
            ("RAMEN_OTEL_ENDPOINT", "http://phoenix.ramen-system:4318"),
            ("RAMEN_REDIS_ITEM_URL", "redis://:pw@redis:6379"),
            ("RAMEN_REDIS_SCOPE_URL", "rediss://:pw@redis:6379"),
            ("RAMEN_TLS_CA", "/etc/ramen/tls/ca.crt"),
            ("RAMEN_TLS_SERVER_NAME", "w.example"),
        ]);
        let c = Config::from_map(&m(&with)).unwrap();
        assert!(c.otel_endpoint.is_none() && c.redis_item_url.is_none());
        assert_eq!(
            c.redis_scope_url.as_deref(),
            Some("rediss://:pw@redis:6379")
        );
        assert_eq!(
            c.plaintext_refused,
            vec!["RAMEN_OTEL_ENDPOINT", "RAMEN_REDIS_ITEM_URL"]
        );
        let e = c.startup_error().unwrap();
        assert!(
            e.contains("RAMEN_OTEL_ENDPOINT") && !e.contains("phoenix"),
            "{e}"
        );
        assert!(ok.startup_error().is_none());
        assert_eq!(c.tls_ca, Some(PathBuf::from("/etc/ramen/tls/ca.crt")));
        assert_eq!(c.tls_server_name, "w.example");
        // without the flag nothing changes (unit tests and laptop runs)
        let c = Config::from_map(&m(&[
            ("RAMEN_OTEL_ENDPOINT", "http://x:4318"),
            ("RAMEN_CONSOLE_INTERNAL_URL", "http://console:80"),
        ]))
        .unwrap();
        assert!(!c.require_tls && c.otel_endpoint.is_some() && c.plaintext_refused.is_empty());
        // the deploy file can neither switch it off nor point the trust elsewhere
        for k in ["RAMEN_REQUIRE_TLS", "RAMEN_TLS_CA", "RAMEN_TLS_SERVER_NAME"] {
            assert!(!DEPLOY_KEYS.contains(&k), "{k}");
        }
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
            "RAMEN_MCP_KEYS=rmk_1,e1\nRAMEN_ALLOWED_CIDRS=10.0.0.0/8\nRAMEN_ADMIN_KEY=nope\nRAMEN_BLOCKED=secret_tool\nRAMEN_TOOL_ACCESS={\"calc\":{\"list\":[\"key\"],\"call\":[\"key\"]}}\nRAMEN_EVALS_KEY=rek_dep\nRAMEN_LLM_PROVIDER=openai\nRAMEN_LLM_MODEL=gpt-4o-mini\nRAMEN_LLM_API_KEY=sk-dep\nAWS_REGION=eu-west-1\nRAMEN_PYTHON=/evil\n",
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
        assert_eq!(c.evals_key.as_deref(), Some("rek_dep")); // §31.4a: the deploy carries the evals key
        // §36.2: the deploy's LLM credential and cloud region reach the runtime env; the key never a Debug line
        assert_eq!(
            c.runtime_env.iter().collect::<Vec<_>>(),
            vec![
                ("AWS_REGION", "eu-west-1"),
                ("RAMEN_LLM_API_KEY", "sk-dep"),
                ("RAMEN_LLM_MODEL", "gpt-4o-mini"),
                ("RAMEN_LLM_PROVIDER", "openai"),
            ]
        );
        let shown = format!("{:?}", c.runtime_env);
        assert!(
            shown.contains("gpt-4o-mini") && !shown.contains("sk-dep"),
            "{shown}"
        );
        assert!(!format!("{c:?}").contains("sk-dep"));
        assert_ne!(c.python, "/evil"); // the interpreter is never the deploy's to set
        for k in RUNTIME_KEYS {
            assert!(DEPLOY_KEYS.contains(k), "{k}");
        }
        assert!(
            Config::from_map(&HashMap::new())
                .unwrap()
                .runtime_env
                .0
                .is_empty()
        );
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

    /// §23 (1.0.0): the five keys of the 2026-07-28 surface, their defaults, and which a group may set.
    #[test]
    fn modern_keys_have_contract_defaults() {
        let c = Config::from_map(&HashMap::new()).unwrap();
        assert_eq!(
            (
                c.list_ttl_ms,
                c.mrtr_ttl_secs,
                c.task_ttl_secs,
                c.max_tasks,
                c.max_subscriptions
            ),
            (60_000, 600, 3600, 4, 64)
        );
        let m: HashMap<_, _> = [
            ("RAMEN_LIST_TTL_MS", "0"),
            ("RAMEN_MRTR_TTL_SECS", "0"),
            ("RAMEN_TASK_TTL_SECS", "5"),
            ("RAMEN_MAX_TASKS", "0"),
            ("RAMEN_MAX_SUBSCRIPTIONS", "2"),
        ]
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
        let c = Config::from_map(&m).unwrap();
        assert_eq!(
            (
                c.list_ttl_ms,
                c.mrtr_ttl_secs,
                c.task_ttl_secs,
                c.max_tasks,
                c.max_subscriptions
            ),
            (0, 1, 5, 0, 2)
        );
        for k in [
            "RAMEN_LIST_TTL_MS",
            "RAMEN_TASK_TTL_SECS",
            "RAMEN_MAX_TASKS",
            "RAMEN_MAX_SUBSCRIPTIONS",
        ] {
            assert!(DEPLOY_KEYS.contains(&k), "{k}");
        }
        assert!(!DEPLOY_KEYS.contains(&"RAMEN_MRTR_TTL_SECS"));
        let m: HashMap<_, _> = [("RAMEN_MAX_TASKS".to_string(), "lots".to_string())].into();
        assert!(
            Config::from_map(&m)
                .unwrap_err()
                .contains("RAMEN_MAX_TASKS")
        );
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

    /// §24 (1.0.0): activator, OpenTelemetry and consent keys.
    #[test]
    fn sleep_and_telemetry_keys_parse() {
        let c = Config::from_map(&HashMap::new()).unwrap();
        assert!(!c.activator && c.console_internal_url.is_none() && c.otel_endpoint.is_none());
        assert_eq!(
            (c.activator_upstream.as_str(), c.wake_timeout_secs),
            ("worker-direct:8080", 25)
        );
        assert!(c.otel_headers.is_empty() && c.otel_client.is_none() && !c.telemetry_consent);
        let m: HashMap<_, _> = [
            ("RAMEN_ACTIVATOR", "1"),
            (
                "RAMEN_CONSOLE_INTERNAL_URL",
                "http://console.ramen-system:8000/",
            ),
            ("RAMEN_ACTIVATOR_UPSTREAM", "http://w:9/"),
            ("RAMEN_WAKE_TIMEOUT_SECS", "0"),
            ("RAMEN_OTEL_ENDPOINT", "http://jaeger:4318/"),
            (
                "RAMEN_OTEL_HEADERS",
                r#"{"x-api-key": "abc", "authorization": "Bearer t"}"#,
            ),
            (
                "RAMEN_OTEL_CLIENT",
                r#"{"otlp_endpoint": "http://jaeger:4318", "headers": {}}"#,
            ),
            ("RAMEN_TELEMETRY_CONSENT", "1"),
            ("RAMEN_TELEMETRY_NOTICE", "jaeger"),
        ]
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
        let c = Config::from_map(&m).unwrap();
        assert!(c.activator && c.telemetry_consent);
        assert_eq!(
            c.console_internal_url.as_deref(),
            Some("http://console.ramen-system:8000")
        );
        assert_eq!(
            (c.activator_upstream.as_str(), c.wake_timeout_secs),
            ("http://w:9", 1)
        );
        assert_eq!(c.otel_endpoint.as_deref(), Some("http://jaeger:4318"));
        assert_eq!(
            c.otel_headers,
            vec![
                ("authorization".to_string(), "Bearer t".to_string()),
                ("x-api-key".to_string(), "abc".to_string())
            ]
        );
        assert_eq!(
            c.otel_client.unwrap()["otlp_endpoint"],
            "http://jaeger:4318"
        );
        assert_eq!(c.telemetry_notice, "jaeger");
        // §26.3: the version the console named, else the one inside the client object, else none
        assert_eq!(c.telemetry_version, "");
        let mut m2 = m.clone();
        m2.insert(
            "RAMEN_OTEL_CLIENT".into(),
            r#"{"otlp_endpoint": "x", "version": "abc123"}"#.into(),
        );
        assert_eq!(Config::from_map(&m2).unwrap().telemetry_version, "abc123");
        m2.insert("RAMEN_TELEMETRY_VERSION".into(), "def456".into());
        assert_eq!(Config::from_map(&m2).unwrap().telemetry_version, "def456");
        // §29.3: the evals version and switch; unset = none known, not required
        assert_eq!(c.evals_version, "");
        assert!(!c.evals_required);
        m2.insert("RAMEN_EVALS_VERSION".into(), " ev0123456789 ".into());
        m2.insert("RAMEN_EVALS_REQUIRED".into(), "true".into());
        let c2 = Config::from_map(&m2).unwrap();
        assert_eq!(c2.evals_version, "ev0123456789");
        assert!(c2.evals_required);
        m2.insert("RAMEN_EVALS_REQUIRED".into(), "0".into());
        assert!(!Config::from_map(&m2).unwrap().evals_required);
        // §30.3: the skillspector version and switch, the same way
        assert_eq!(c.skillspector_version, "");
        assert!(!c.skillspector_required);
        m2.insert("RAMEN_SKILLSPECTOR_VERSION".into(), " ss0123456789 ".into());
        m2.insert("RAMEN_SKILLSPECTOR_REQUIRED".into(), "1".into());
        let c3 = Config::from_map(&m2).unwrap();
        assert_eq!(c3.skillspector_version, "ss0123456789");
        assert!(c3.skillspector_required);
        m2.insert("RAMEN_SKILLSPECTOR_REQUIRED".into(), "no".into());
        assert!(!Config::from_map(&m2).unwrap().skillspector_required);
        // §31.2: the configs version and switch, the same way
        assert_eq!(c.configs_version, "");
        assert!(!c.configs_required);
        m2.insert("RAMEN_CONFIGS_VERSION".into(), " cf0123456789 ".into());
        m2.insert("RAMEN_CONFIGS_REQUIRED".into(), "1".into());
        let c4 = Config::from_map(&m2).unwrap();
        assert_eq!(c4.configs_version, "cf0123456789");
        assert!(c4.configs_required);
        m2.insert("RAMEN_CONFIGS_REQUIRED".into(), "0".into());
        assert!(!Config::from_map(&m2).unwrap().configs_required);
        // §36.3: the guardrails version and switch, the same way
        assert_eq!(c.guardrails_version, "");
        assert!(!c.guardrails_required);
        m2.insert("RAMEN_GUARDRAILS_VERSION".into(), " gr0123456789 ".into());
        m2.insert("RAMEN_GUARDRAILS_REQUIRED".into(), "true".into());
        let c5 = Config::from_map(&m2).unwrap();
        assert_eq!(c5.guardrails_version, "gr0123456789");
        assert!(c5.guardrails_required);
        m2.insert("RAMEN_GUARDRAILS_REQUIRED".into(), "0".into());
        assert!(!Config::from_map(&m2).unwrap().guardrails_required);
        // a bad value is refused without being quoted back
        for (k, v) in [
            ("RAMEN_OTEL_HEADERS", r#"{"x": 1}"#),
            ("RAMEN_OTEL_HEADERS", r#"{"bad name": "s3cr3t"}"#),
            ("RAMEN_OTEL_HEADERS", "s3cr3t"),
            ("RAMEN_OTEL_CLIENT", "[1]"),
        ] {
            let m: HashMap<_, _> = [(k.to_string(), v.to_string())].into();
            let e = Config::from_map(&m).unwrap_err();
            assert!(e.contains(k) && !e.contains("s3cr3t"), "{e}");
        }
        // a group's deploy file may carry the telemetry settings, never the activator's
        for k in [
            "RAMEN_OTEL_ENDPOINT",
            "RAMEN_OTEL_HEADERS",
            "RAMEN_OTEL_CLIENT",
            "RAMEN_TELEMETRY_CONSENT",
            "RAMEN_TELEMETRY_NOTICE",
            "RAMEN_TELEMETRY_VERSION",
            "RAMEN_EVALS_VERSION",
            "RAMEN_EVALS_REQUIRED",
            "RAMEN_SKILLSPECTOR_VERSION",
            "RAMEN_SKILLSPECTOR_REQUIRED",
            "RAMEN_CONFIGS_VERSION",
            "RAMEN_CONFIGS_REQUIRED",
        ] {
            assert!(DEPLOY_KEYS.contains(&k), "{k}");
        }
        for k in [
            "RAMEN_ACTIVATOR",
            "RAMEN_CONSOLE_INTERNAL_URL",
            "RAMEN_ACTIVATOR_UPSTREAM",
            "RAMEN_WAKE_TIMEOUT_SECS",
        ] {
            assert!(!DEPLOY_KEYS.contains(&k), "{k}");
        }
    }
}
