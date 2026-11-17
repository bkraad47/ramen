"""D84: the access matrix of every 1.0.0c section — one table that `conformance/test_security_matrix.py` (local) and
`cloud/test_auth_matrix.py` (a live deployment) both walk, so the rule of `docs/wiki/users-access.md` is asserted per
role and per route: edit = a super admin or that group's admin; view = the group's members; an outsider (a member of
another group) is refused like the older matrix (403, or 404 for a page that hides the group); MCP users get the
configs page and the accept API (any member, §31.1) and nothing else of the console; `devops` API keys follow the role
and groups they were minted with; `agent` keys are refused everywhere (D21). The OTLP ingest has its own callers
(a bearer: user token, MCP key or session, §31.4) and is walked by `otlp_rows`.

A row is `(name, call, expected)`: `call(console, group)` makes one request and `expected` maps a caller kind to the
status codes that prove the gate for it — the "allowed" codes include what a group without a deploy answers (404 for a
scan report or an app nobody served, 409/422 for a stale configs version), since a refusal always comes first."""

from collections.abc import Callable

import httpx

from .console import Console

Call = Callable[[Console, str], httpx.Response]
Row = tuple[str, Call, dict[str, tuple[int, ...]]]

KINDS = (
    "anon",
    "outsider",
    "viewer",
    "mcp_user",
    "group_admin",
    "super_admin",
    "devops_viewer",
    "devops_admin",
    "agent",
)
PAGE_KINDS = ("anon", "outsider", "viewer", "mcp_user", "group_admin", "super_admin", "agent")  # keys are for the API
PROVIDER = "openai"  # a provider name every console knows; the models route answers `provider not set` when unset


def _expected(ok: tuple[int, ...], allowed: tuple[str, ...], anon: int, kinds=KINDS) -> dict[str, tuple[int, ...]]:
    return {k: (ok if k in allowed else ((anon,) if k == "anon" else (403,))) for k in kinds}


def super_only(ok=(200,)) -> dict:
    return _expected(ok, ("super_admin",), 401)


def group_admin(ok=(200,)) -> dict:
    return _expected(ok, ("group_admin", "super_admin", "devops_admin"), 401)


def group_admin_page(ok=(200,)) -> dict:
    return _expected(ok, ("group_admin", "super_admin"), 303, PAGE_KINDS)


def member_page(ok=(200,)) -> dict:
    return _expected(ok, ("viewer", "group_admin", "super_admin"), 303, PAGE_KINDS)


def any_member_page(ok=(200,)) -> dict:
    return _expected(ok, ("viewer", "mcp_user", "group_admin", "super_admin"), 303, PAGE_KINDS)


def any_member_api(ok=(200,)) -> dict:
    return _expected(ok, ("viewer", "mcp_user", "group_admin", "super_admin", "devops_viewer", "devops_admin"), 401)


def page(c: Console, key: str, **kw) -> httpx.Response:
    return c.page(c.url(key, **kw))


SECTIONS: list[Row] = [
    # org LLM providers (+ models): super admin
    ("org providers: GET /config/llm-providers", lambda c, g: c.get("config_llm_providers"), super_only()),
    ("org providers: PUT /config/llm-providers", lambda c, g: c.put("config_llm_providers", {}), super_only()),
    (
        "org providers: GET /config/llm-providers/{p}/models",
        lambda c, g: c.get("config_llm_models", provider=PROVIDER),
        super_only(),
    ),
    # group LLM providers (+ models), guardrails, evals, skillspector settings: the group's admin
    (
        "group providers: GET /groups/{g}/llm-providers",
        lambda c, g: c.get("group_llm_providers", group=g),
        group_admin(),
    ),
    (
        "group providers: PUT /groups/{g}/llm-providers",
        lambda c, g: c.put("group_llm_providers", {}, group=g),
        group_admin(),
    ),
    (
        "group providers: GET /groups/{g}/llm-providers/{p}/models",
        lambda c, g: c.get("group_llm_models", group=g, provider=PROVIDER),
        group_admin(),
    ),
    (
        "group guardrails: PUT /groups/{g}/guardrails",
        lambda c, g: c.put("group_guardrails", {}, group=g),
        group_admin(),
    ),
    ("group evals: PUT /groups/{g}/evals", lambda c, g: c.put("group_evals", {}, group=g), group_admin()),
    (
        "group scans: PUT /groups/{g}/skillspector",
        lambda c, g: c.put("group_skillspector", {}, group=g),
        group_admin(),
    ),
    (
        "policy cards' model slot: GET /ui/groups/{g}/guardrails/models",
        lambda c, g: page(c, "guardrail_models_ui", group=g),
        group_admin_page(),
    ),
    # pages for members: the group page (packages, apps and skills of every zone), a scan or evals report, an app
    # preview
    ("group page (apps and skills): GET /groups/{g}", lambda c, g: page(c, "group_page", group=g), member_page()),
    (
        "scan report page: GET /groups/{g}/scans/{job}/{zone}",
        lambda c, g: page(c, "scan_report", group=g, job="no-such-job", zone="any"),
        member_page((200, 404)),
    ),
    (
        "evals report page: GET /groups/{g}/evals/{job}/{zone}",
        lambda c, g: page(c, "evals_report", group=g, job="no-such-job", zone="any"),
        member_page((200, 404)),
    ),
    (
        "app preview: GET /groups/{g}/apps/{zone}/{app}",
        lambda c, g: page(c, "app_preview", group=g, zone="any", name="no-such-app"),
        member_page((200, 404, 502)),
    ),
    # LLM configs: any member, MCP users included (§31.1)
    (
        "configs page: GET /groups/{g}/configs",
        lambda c, g: page(c, "configs_page", group=g),
        any_member_page((200, 404)),
    ),
    (
        "configs accept: POST /api/v1/groups/{g}/configs/accept",
        lambda c, g: c.post("configs_accept", {"version": "stale"}, group=g),
        any_member_api((200, 404, 409, 422)),
    ),
]

OTLP_OK = (200, 202, 204)  # 204 while telemetry is off, the collector's answer when it is on


def otlp_rows(mcp_key: str, group: str) -> list[tuple[str, str, Callable[[Console], httpx.Response], tuple[int, ...]]]:
    """§31.4 `POST /otlp/v1/traces`, a row per caller: `(name, caller kind, call(console), expected)`. A session names
    its group in `ramen-group` (any member, MCP users included); a console API key (devops or agent) is not a
    telemetry credential (401/403); an unknown or foreign MCP key is 401."""
    body = {"resourceSpans": []}
    named = {"ramen-group": group}
    return [
        ("no credential", "anon", lambda c: c.otlp("traces", None, body), (401,)),
        ("MCP key of the group (bearer)", "anon", lambda c: c.otlp("traces", mcp_key, body), OTLP_OK),
        ("unknown MCP key", "anon", lambda c: c.otlp("traces", "rmk_nope_nope", body), (401,)),
        ("session without a group", "viewer", lambda c: c.otlp("traces", None, body), (401,)),
        ("session + ramen-group (viewer)", "viewer", lambda c: c.otlp("traces", None, body, **named), OTLP_OK),
        ("session + ramen-group (MCP user)", "mcp_user", lambda c: c.otlp("traces", None, body, **named), OTLP_OK),
        (
            "session + ramen-group (group admin)",
            "group_admin",
            lambda c: c.otlp("traces", None, body, **named),
            OTLP_OK,
        ),
        ("session + ramen-group (outsider)", "outsider", lambda c: c.otlp("traces", None, body, **named), (403,)),
        ("devops API key header", "devops_admin", lambda c: c.otlp("traces", None, body, **named), (401, 403)),
        ("agent API key header", "agent", lambda c: c.otlp("traces", None, body, **named), (401, 403)),
    ]
