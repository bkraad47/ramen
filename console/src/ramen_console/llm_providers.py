"""§29.1 / §30.1 Config → LLM providers: `config/llm_providers = {anthropic: {api_key}, openai: {api_key, base_url?},
azure_openai: {api_key, host}, vertex: {project, location}, bedrock: {region}, foundry: {endpoint, api_key, deployment},
openai_compatible: {base_url, api_key, headers}}`. Each `api_key` is a console secret (§24.5: vaulted on the clouds,
Fernet locally — `storage/encrypted.py` NESTED), shown as set/not set; `headers` (a flat JSON object, stored as its
text) is write-only too. Fed to the evals and scan Jobs as env, never written to a log, a job result or a client."""

import json
import re
from urllib.parse import urlsplit

from . import redact
from .security import remember_secret
from .tls import require_tls

DOC = "llm_providers"
MODELS = "allowed_models"  # §36.2: per provider, a list of model ids; empty = the provider's default only
# provider → (credential fields, default model); None = no default (the first allowed model, or an error)
TABLE: dict[str, tuple[tuple[str, ...], str | None]] = {
    "anthropic": (("api_key",), "claude-haiku-4-5"),
    "openai": (("api_key", "base_url"), "gpt-4o-mini"),
    "azure_openai": (("api_key", "host"), None),  # the deployment name: an allowed model
    "vertex": (("project", "location"), "gemini-2.0-flash"),
    "bedrock": (("region",), "anthropic.claude-3-haiku-20240307-v1:0"),
    "foundry": (("api_key", "endpoint", "deployment"), "deployment"),  # = the `deployment` field
    "openai_compatible": (("api_key", "base_url", "headers"), None),
}
FIELDS = {p: (*fields, MODELS) for p, (fields, _) in TABLE.items()}
# what a provider needs before it can be used (§36.2 "configured"): the key or the cluster identity's coordinates
REQUIRED = {
    "anthropic": ("api_key",),
    "openai": ("api_key",),
    "azure_openai": ("api_key", "host"),
    "vertex": ("project",),
    "bedrock": ("region",),
    "foundry": ("api_key", "endpoint"),
    "openai_compatible": ("base_url",),
}
SECRET = "api_key"
WRITE_ONLY = (SECRET, "headers")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")
ENV = {
    ("anthropic", "api_key"): "ANTHROPIC_API_KEY",
    ("openai", "api_key"): "OPENAI_API_KEY",
    ("openai", "base_url"): "OPENAI_BASE_URL",
    ("azure_openai", "api_key"): "AZURE_API_KEY",
    ("azure_openai", "host"): "AZURE_API_HOST",
    ("vertex", "project"): "GOOGLE_CLOUD_PROJECT",
    ("vertex", "location"): "GOOGLE_CLOUD_LOCATION",
    ("bedrock", "region"): "AWS_REGION",
    ("foundry", "endpoint"): "AZURE_OPENAI_ENDPOINT",
    ("foundry", "api_key"): "AZURE_OPENAI_API_KEY",
    ("foundry", "deployment"): "AZURE_OPENAI_DEPLOYMENT",
    ("openai_compatible", "base_url"): "SKILLSPECTOR_COMPAT_BASE_URL",
    ("openai_compatible", "api_key"): "SKILLSPECTOR_COMPAT_API_KEY",
    ("openai_compatible", "headers"): "SKILLSPECTOR_COMPAT_HEADERS",
}
HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$", re.I)
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
URL_FIELDS = ("base_url", "endpoint")
TOKEN_FIELDS = ("project", "location", "region", "deployment")


METADATA_NAMES = {
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "instance-data.ec2.internal",
}


def metadata_host(host: str) -> bool:
    """A cloud metadata service (GCP / AWS / Azure: link-local 169.254.0.0/16, fe80::/10, AWS's fd00:ec2::254, or one
    of their names): no LLM server lives there, and the console's `/models` GET would carry its identity there."""
    import ipaddress

    host = (host or "").strip("[]").rstrip(".").lower()
    if host in METADATA_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_link_local or ip == ipaddress.ip_address("fd00:ec2::254")


def _models(provider: str, value) -> list[str]:
    """`allowed_models`: a list, or the form's comma/space-separated text (the card posts the picked ids plus a
    free-text field, so a list item may itself be a comma list); each a model id; [] clears."""
    if isinstance(value, str):
        value = [v for v in re.split(r"[,\s]+", value) if v]
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        value = [m.strip() for v in value for m in v.split(",") if m.strip()]
    if not isinstance(value, list) or not all(isinstance(v, str) and MODEL.match(v.strip()) for v in value):
        raise ValueError(f"{provider}.{MODELS} must be a list of model ids (letters, digits, . _ : / @ -)")
    return list(dict.fromkeys(v.strip() for v in value))


def _clean(provider: str, field: str, value):
    if field == "headers":
        return _headers(provider, value)
    if field == MODELS:
        return _models(provider, value)
    value = str(value).strip()
    if not value:
        return ""
    if field in URL_FIELDS:
        u = urlsplit(value)
        if u.scheme not in ("http", "https") or not u.hostname or any(c.isspace() for c in value):
            raise ValueError(f"{provider}.{field} must be an http(s):// URL")
        if u.scheme != "https" and require_tls():  # D86 §39.4
            raise ValueError(f"{provider}.{field} must be an https:// URL on this install (plaintext is refused)")
        if metadata_host(u.hostname):
            raise ValueError(f"{provider}.{field} must not point at the cloud metadata service")
        return value.rstrip("/")
    if field == "host" and not HOST.match(value):
        raise ValueError(f"{provider}.host must be a hostname like my-resource.openai.azure.com")
    if field in TOKEN_FIELDS and not TOKEN.match(value):
        raise ValueError(f"{provider}.{field} must be a plain identifier (letters, digits, . _ : -)")
    return value


def _headers(provider: str, value) -> str:
    """A flat JSON object of header name → value, stored as compact JSON text; "" or {} clears."""
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return ""
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError(f"{provider}.headers must be a JSON object") from None
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and HOST.match(k.replace("_", "-") or "-") and isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError(f"{provider}.headers must be a flat JSON object of header name to string value")
    return json.dumps(value, sort_keys=True) if value else ""


def merge(current: dict, body: dict) -> dict:
    """The stored doc after a PUT: a field that is None (or absent) keeps its value, "" clears it; a provider with no
    field left disappears. Raises ValueError for an unknown provider or field, or a malformed value."""
    out = {p: dict(v) for p, v in (current or {}).items() if isinstance(v, dict)}
    for provider, fields in (body or {}).items():
        if provider not in FIELDS:
            raise ValueError(f"unknown provider {provider!r}: one of {', '.join(FIELDS)}")
        if not isinstance(fields, dict):
            raise ValueError(f"{provider} must be a mapping of {', '.join(FIELDS[provider])}")
        entry = out.get(provider, {})
        for field, value in fields.items():
            if field not in FIELDS[provider]:
                raise ValueError(f"{provider} has no field {field!r}")
            if value is None:
                continue
            if field in URL_FIELDS:  # N1: the card shows the URL masked; posting that back keeps the stored one
                value = redact.keep(value, entry.get(field))
            cleaned = _clean(provider, field, value)
            if cleaned:
                entry[field] = cleaned
            else:
                entry.pop(field, None)
        if entry:
            out[provider] = entry
        else:
            out.pop(provider, None)
    return out


def env(cfg: dict) -> dict[str, str]:
    """The provider env the evals and scan Jobs get (§29.2 / §30.1): only providers that are set are exported."""
    return {ENV[p, f]: v for p, fields in (cfg or {}).items() for f, v in fields.items() if (p, f) in ENV and v}


def is_configured(cfg: dict, provider: str) -> bool:
    """§36.2: the provider has what it needs to be used (its key, or the cluster identity's coordinates)."""
    entry = (cfg or {}).get(provider) or {}
    return provider in REQUIRED and all(entry.get(f) for f in REQUIRED[provider])


def configured(cfg: dict) -> list[str]:
    return sorted(p for p in FIELDS if is_configured(cfg, p))


def default_model(provider: str, entry: dict | None = None) -> str | None:
    """The model used when none is named: the TABLE default, foundry's `deployment`, else the first allowed model
    (None when there is none either)."""
    entry = entry or {}
    _, default = TABLE[provider]
    if default == "deployment":
        default = entry.get("deployment") or None
    return default or (entry.get(MODELS) or [None])[0]


def allowed_models(scope_cfg: dict, provider: str) -> list[str]:
    """The model ids a scope allows for a provider: its `allowed_models`, or the default alone when the list is empty
    ([] when the provider has no default either — an allowed model must then be set)."""
    entry = (scope_cfg or {}).get(provider) or {}
    if entry.get(MODELS):
        return list(entry[MODELS])
    d = default_model(provider, entry)
    return [d] if d else []


def public(cfg: dict) -> dict:
    """What clients and the Config page see: every provider, the key (and headers) as set/not set, the plain fields
    as they are, `allowed_models` as the list."""
    out = {}
    for provider, fields in FIELDS.items():
        entry = (cfg or {}).get(provider) or {}
        out[provider] = {"api_key_set": bool(entry.get(SECRET))} if SECRET in fields else {}
        for f in fields:
            if f == "headers":
                out[provider]["headers_set"] = bool(entry.get(f))
            elif f == MODELS:
                out[provider][f] = list(entry.get(f) or [])
            elif f in URL_FIELDS:
                out[provider][f] = redact.redact(entry.get(f) or "")  # N1: a credential in the URL is masked
            elif f != SECRET:
                out[provider][f] = entry.get(f) or ""
    return out


def from_form(data: dict) -> dict:
    """The Config cards post `<provider>_<field>` fields; an empty write-only field (key, headers) means "unchanged"."""
    out: dict[str, dict] = {}
    for provider, fields in FIELDS.items():
        for f in fields:
            if (k := f"{provider}_{f}") in data:
                v = data[k]
                if f in WRITE_ONLY and not (v or "").strip():
                    continue
                out.setdefault(provider, {})[f] = v
    return out


def headers_of(entry: dict) -> dict[str, str]:
    """The stored `headers` text as a dict ({} when unset)."""
    try:
        h = json.loads(entry.get("headers") or "{}")
    except ValueError:
        return {}
    return h if isinstance(h, dict) else {}


LABELS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "azure_openai": "Azure OpenAI",
    "vertex": "Vertex AI",
    "bedrock": "Bedrock",
    "foundry": "Azure AI Foundry",
    "openai_compatible": "OpenAI-compatible",
}


def card(cfg: dict) -> dict:
    """What the one LLM providers card (org or group) renders: `public`, the configured names, each provider's default
    model (None when it has none)."""
    return {
        "providers": public(cfg),
        "configured": configured(cfg),
        "defaults": {p: default_model(p, (cfg or {}).get(p)) for p in FIELDS},
    }


def base_urls(cfg: dict) -> list[str]:
    """The base URLs a promptfoo `openai:` provider may point at (§33.4a): the OpenAI and compatible-server ones."""
    return [v[f] for v in (cfg or {}).values() for f in URL_FIELDS if isinstance(v, dict) and v.get(f)]


def _remember(cfg: dict) -> dict:
    """§33.4a: every key (and header value) is scrubbed from logs, job records and pages from here on."""
    for v in (cfg or {}).values():
        for f in WRITE_ONLY:
            remember_secret(v.get(f)) if isinstance(v, dict) else None
    return cfg


def group_doc(group: str) -> str:
    """§36.2: a group's own providers live at `config/groups/<g>/llm_providers` (same NESTED `api_key` encryption)."""
    return f"groups/{group}/{DOC}"


async def load(store, group: str | None = None) -> dict:
    """The org providers, or one group's when `group` is given."""
    doc = await store.get("config", group_doc(group) if group else DOC) or {}
    return _remember({p: dict(v) for p, v in doc.items() if p in FIELDS and isinstance(v, dict)})


async def save(store, cfg: dict, group: str | None = None) -> dict:
    await store.put("config", group_doc(group) if group else DOC, _remember(cfg))
    return cfg
