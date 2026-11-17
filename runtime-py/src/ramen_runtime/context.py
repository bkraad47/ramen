"""What tool and prompt code reaches for during a call (CONTRACTS §23.3, §23.4): input requests, the client's
answers, the verified state, the declared capabilities and progress. Per call, on a contextvar, so a task thread and
the serving thread never see each other's call."""

import json
import re
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from . import secrets

METHODS = ("elicitation/create", "sampling/createMessage", "roots/list")
# `card` and `ssn` only as a whole word of the name (`card_number`, `cardNumber`), so `discard` stays askable
_SECRET = re.compile(r"password|passwd|secret|token|api[_-]?key|credential|cvv", re.I)
_SECRET_WORDS = {"card", "ssn"}
_WORDS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def _secret_like(name: str) -> bool:
    return bool(_SECRET.search(name)) or any(w.lower() in _SECRET_WORDS for w in _WORDS.findall(name))


class InputRequired(Exception):  # noqa: N818 - the contract's name; it is an outcome, not an error
    """Raise from a tool or callable prompt: `requests` maps a key to an `elicit`/`elicit_url`/`sample`/`list_roots`
    request; `state` (any JSON) comes back verified from `request_state()` on the retry."""

    def __init__(self, requests: dict[str, dict] | None = None, state=None):
        super().__init__("input required")
        self.requests, self.state = requests or {}, state


class MissingCapability(Exception):  # noqa: N818
    """Raise when the tool cannot run for this client; `required` is the capabilities object it needs (-32021)."""

    def __init__(self, required: dict):
        super().__init__("missing capability")
        self.required = required


@dataclass
class Call:
    responses: dict = field(default_factory=dict)
    state: object = None
    capabilities: dict = field(default_factory=dict)
    sink: Callable[[dict], None] | None = None
    used: list[str] = field(default_factory=list)  # secret values resolved for this call, for redaction


_CALL: ContextVar[Call | None] = ContextVar("ramen_call", default=None)


@contextmanager
def call(responses=None, state=None, client=None, sink=None, used=None):
    token = _CALL.set(Call(responses or {}, state, client or {}, sink, used if used is not None else []))
    try:
        yield
    finally:
        _CALL.reset(token)


def input_responses() -> dict:
    return c.responses if (c := _CALL.get()) else {}


def request_state():
    return c.state if (c := _CALL.get()) else None


def client_capabilities() -> dict:
    return c.capabilities if (c := _CALL.get()) else {}


def progress(progress: float, total: float | None = None, message: str | None = None) -> None:
    """No-op unless the request asked for progress (`_meta.progressToken`); never sent for a task."""
    if not (c := _CALL.get()) or c.sink is None:
        return
    p: dict = {"progress": progress}
    if total is not None:
        p["total"] = total
    if message is not None:
        p["message"] = secrets.redact(str(message), hidden(c.used))
    c.sink(p)


def elicit(message: str, schema: dict) -> dict:
    return {"method": "elicitation/create", "params": {"mode": "form", "message": message, "requestedSchema": schema}}


def elicit_url(message: str, url: str) -> dict:
    return {"method": "elicitation/create", "params": {"mode": "url", "message": message, "url": url}}


def sample(messages, max_tokens: int = 100, **params) -> dict:
    if isinstance(messages, str):
        messages = [{"role": "user", "content": {"type": "text", "text": messages}}]
    extra = {re.sub(r"_([a-z])", lambda m: m.group(1).upper(), k): v for k, v in params.items()}
    return {"method": "sampling/createMessage", "params": {"messages": messages, "maxTokens": max_tokens} | extra}


def list_roots() -> dict:
    return {"method": "roots/list", "params": {}}


def hidden(used: list[str]) -> list[str]:
    from . import envfile

    return used + envfile.rendered_values()


def refusal(e: InputRequired) -> str | None:
    """Why this input request cannot go out, or None. A form that asks for a credential is refused (§23.3)."""
    if not isinstance(e.requests, dict) or (not e.requests and e.state is None):
        return "input request invalid: needs requests or state"
    for key, req in e.requests.items():
        if not isinstance(req, dict) or req.get("method") not in METHODS:
            return f"input request invalid: {key} is not an elicit, sample or roots request"
        params = req.get("params") if isinstance(req.get("params"), dict) else {}
        if req["method"] == "elicitation/create" and params.get("mode", "form") != "url":
            schema = params.get("requestedSchema")
            for name in (schema.get("properties") or {}) if isinstance(schema, dict) else ():
                if _secret_like(str(name)):
                    return f"input request refused: {key}.{name} looks like a secret; use elicit_url"
    return None


def input_required(e: InputRequired, used: list[str]) -> dict:
    """The §23.4 sidecar result; resolved secret values are redacted from what leaves the runtime."""
    r = {"resultType": "input_required", "inputRequests": _clean(e.requests, used)}
    return r | ({"state": _clean(e.state, used)} if e.state is not None else {})


def missing_capability(e: MissingCapability) -> dict:
    return {"resultType": "missing_capability", "requiredCapabilities": _clean(e.required, [])}


def _clean(value, used: list[str]):
    return secrets.redact_deep(json.loads(json.dumps(value)), hidden(used))
