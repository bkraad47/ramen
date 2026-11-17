"""§36.2 scoped LLM providers: a group's own providers (`config/groups/<g>/llm_providers`) win over the org's
(`config/llm_providers`) for the group's guardrails, SkillSpector scans and promptfoo evals; the model must be one the
chosen scope allows (`allowed_models`, or the provider's default). No provider anywhere is an error, never a silent
skip: `no LLM provider: set one on the group or ask a super admin`."""

from . import llm_providers as lp
from .security import remember_secret

MESSAGE = "no LLM provider: set one on the group or ask a super admin"


class NoProvider(RuntimeError):
    def __init__(self, purpose: str = "", provider: str | None = None):
        what = f"{purpose}: " if purpose else ""
        want = f" ({provider!r} is set on neither the group nor the org)" if provider else ""
        super().__init__(f"{what}{MESSAGE}{want}")


class BadModel(ValueError):
    """The named model is not in the chosen scope's `allowed_models` (a 422 at the settings route)."""


def scopes(group_cfg: dict, org_cfg: dict) -> list[tuple[str, dict]]:
    return [("group", group_cfg or {}), ("org", org_cfg or {})]


def pick_scope(group_cfg: dict, org_cfg: dict, provider: str | None = None) -> tuple[str, dict] | None:
    """The first scope that configures `provider` (any provider when None): group, then org; None when neither."""
    for name, cfg in scopes(group_cfg, org_cfg):
        if lp.is_configured(cfg, provider) if provider else lp.configured(cfg):
            return name, cfg
    return None


def configured(group_cfg: dict, org_cfg: dict) -> list[str]:
    """Every provider some scope configures (what a group setting may name)."""
    return sorted(set(lp.configured(group_cfg)) | set(lp.configured(org_cfg)))


def options(group_cfg: dict, org_cfg: dict, provider: str | None = None) -> dict | None:
    """The group page's guardrails model select: `{scope, provider, models}` of the scope that would serve `provider`
    (the first configured one when None); None when no scope configures it."""
    picked = pick_scope(group_cfg, org_cfg, provider or None)
    if picked is None:
        return None
    scope, cfg = picked
    provider = provider or lp.configured(cfg)[0]
    return {"scope": scope, "provider": provider, "models": lp.allowed_models(cfg, provider)}


def resolve(
    group_doc: dict,
    group_cfg: dict,
    org_cfg: dict,
    purpose: str,
    want_provider: str | None = None,
    want_model: str | None = None,
) -> dict:
    """`{scope, provider, model, entry, env}`: the group scope when it configures the wanted provider (or any, when
    none is wanted), else the org scope, else NoProvider. `model` must be allowed by the chosen scope (BadModel names
    the allowed list); without a wanted model the scope's default (None when the provider has none and the purpose
    is not guardrails). `env` is the whole scope's Job env (§29.1 / §30.1 names); the workers get `worker_env`."""
    want_provider = (want_provider or "").strip() or None
    picked = pick_scope(group_cfg, org_cfg, want_provider)
    if picked is None:
        raise NoProvider(purpose, want_provider)
    scope, cfg = picked
    provider = want_provider or lp.configured(cfg)[0]
    entry = dict(cfg.get(provider) or {})
    allowed = lp.allowed_models(cfg, provider)
    model = (want_model or "").strip() or None
    if model and model not in allowed:
        raise BadModel(
            f"model {model!r} is not allowed for {provider} ({scope} scope): one of "
            + (", ".join(allowed) if allowed else f"none yet — set {lp.MODELS} on the provider")
        )
    if model is None:
        model = allowed[0] if allowed else None
        if model is None and purpose == "guardrails":
            raise BadModel(f"{provider} ({scope} scope) has no default model: set {lp.MODELS} on the provider")
    return {"scope": scope, "provider": provider, "model": model, "entry": entry, "env": lp.env(cfg)}


def worker_env(res: dict, base_url: str, token: str) -> dict[str, str]:
    """§38.1: what a zone's workers fill NeMo's `models:` with — the console's judge proxy as an OpenAI-compatible
    server, the zone's judge token as its key and the resolved model; no provider key leaves the console."""
    remember_secret(token)
    return {
        "RAMEN_LLM_PROVIDER": "openai_compatible",
        "RAMEN_LLM_BASE_URL": base_url,
        "RAMEN_LLM_API_KEY": token,
        "RAMEN_LLM_MODEL": res["model"],
    }


def client_llm(res: dict, base_url: str) -> dict:
    """§38.1 `guardrails.client_llm`: the proxy at the console's public URL and the model; the bridge sends the
    person's own access token as the key."""
    return {"provider": "openai_compatible", "base_url": base_url, "model": res["model"]}
