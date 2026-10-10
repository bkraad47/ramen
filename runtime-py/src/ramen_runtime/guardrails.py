"""`mcp/guardrails.yaml` (CONTRACTS §21.2, D46): per-tool pre/post hooks run by an engine inside the runtime.

```yaml
engine: nemo            # nemo | policy | none
config: mcp/guardrails  # nemo: a NeMo rails config dir; policy: the dir holding policy.py
fail: closed            # closed | open — what a hook error or timeout means at call time
timeout_s: 10
tools:
  word_count: {pre: true, post: true}
```
A tool not listed is never checked. `pre` sees the tool name and the *redacted* arguments (secret references as
written, never the values); `post` sees the result. Blocked → an `isError` tool result carrying
`_meta.ramen.guardrail`. An engine that failed to load blocks every opted-in tool whatever `fail` says: a broken
engine never means an unguarded tool. Log lines name tool/stage/verdict, never the payload.
"""

from __future__ import annotations

import importlib.util
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .log import log

FILES = ("guardrails.yaml", "guardrails.yml")
ENGINES = ("none", "nemo", "policy")
STAGES = ("pre", "post")
DEFAULT_TIMEOUT, MAX_TIMEOUT = 10.0, 120.0
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class GuardrailsError(ValueError):
    pass


class HookTimeout(Exception):
    pass


@dataclass
class Config:
    engine: str = "none"
    config: Path | None = None
    config_rel: str = ""
    fail: str = "closed"
    timeout_s: float = DEFAULT_TIMEOUT
    tools: dict[str, set[str]] = field(default_factory=dict)

    def describe(self) -> dict:
        if self.engine == "none":
            return {"engine": "none", "tools": {}}
        return {
            "engine": self.engine,
            "fail": self.fail,
            "tools": {t: [s for s in STAGES if s in stages] for t, stages in sorted(self.tools.items())},
        }


def find(bucket: Path) -> Path | None:
    for name in FILES:
        if (p := bucket / "mcp" / name).is_file():
            return p
    return None


def parse(path: Path, bucket: Path) -> Config:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise GuardrailsError(f"{path.name}: invalid yaml: {e}") from None
    if not isinstance(doc, dict):
        raise GuardrailsError(f"{path.name}: expected a mapping")
    engine = str(doc.get("engine", "none"))
    if engine not in ENGINES:
        raise GuardrailsError(f"{path.name}: engine must be one of {', '.join(ENGINES)}, got {engine!r}")
    fail = str(doc.get("fail", "closed"))
    if fail not in ("closed", "open"):
        raise GuardrailsError(f"{path.name}: fail must be closed or open, got {fail!r}")
    timeout = doc.get("timeout_s", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not 0 < timeout <= MAX_TIMEOUT:
        raise GuardrailsError(f"{path.name}: timeout_s must be a number in (0, {MAX_TIMEOUT:g}], got {timeout!r}")
    tools: dict[str, set[str]] = {}
    for name, spec in (doc.get("tools") or {}).items():
        if not isinstance(name, str) or not _NAME.match(name):
            raise GuardrailsError(f"{path.name}: tools: {name!r} is not a tool name")
        if spec is True:
            spec = {"pre": True}
        if not isinstance(spec, dict) or not all(k in STAGES and isinstance(v, bool) for k, v in spec.items()):
            raise GuardrailsError(f"{path.name}: tools.{name} must be {{pre: bool, post: bool}}")
        if stages := {s for s in STAGES if spec.get(s)}:
            tools[name] = stages
    cfg = Config(engine=engine, fail=fail, timeout_s=float(timeout), tools=tools)
    if engine != "none":
        rel = doc.get("config", "mcp/guardrails")
        if not isinstance(rel, str) or not rel:
            raise GuardrailsError(f"{path.name}: config must be a path inside the repo")
        cfg.config, cfg.config_rel = (bucket / rel).resolve(), rel
        if not cfg.config.is_relative_to(bucket.resolve()):
            raise GuardrailsError(f"{path.name}: config dir {rel!r} is outside the repo")
    return cfg


class PolicyEngine:
    """`policy.py` in the config dir: `pre(tool, arguments)` and/or `post(tool, arguments, result)`, each returning
    `None` (allow) or a message (block)."""

    name = "policy"

    def __init__(self, config: Path):
        py = config / "policy.py"
        if not py.is_file():
            raise GuardrailsError(f"policy engine: {py.relative_to(config.parent.parent)} not found")
        spec = importlib.util.spec_from_file_location("ramen_guardrails_policy", py)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self._pre, self._post = getattr(mod, "pre", None), getattr(mod, "post", None)
        if not callable(self._pre) and not callable(self._post):
            raise GuardrailsError("policy engine: policy.py defines neither pre() nor post()")

    def pre(self, tool: str, arguments: dict) -> str | None:
        return _message(self._pre(tool, arguments)) if self._pre else None

    def post(self, tool: str, arguments: dict, result) -> str | None:
        return _message(self._post(tool, arguments, result)) if self._post else None


def _message(v) -> str | None:
    if v is None or v is False:
        return None
    return str(v) if not isinstance(v, bool) else "blocked by policy"


def make_engine(cfg: Config):
    if cfg.engine != "none" and not (cfg.config and cfg.config.is_dir()):
        raise GuardrailsError(f"config dir {cfg.config_rel!r} not found in the repo")
    if cfg.engine == "policy":
        return PolicyEngine(cfg.config)
    if cfg.engine == "nemo":
        from . import guardrails_nemo

        return guardrails_nemo.NemoEngine(cfg.config)
    return None


def canonical(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class Guard:
    """What the executor asks before and after a tool call. `error` set = the engine did not load."""

    def __init__(self, cfg: Config, engine=None, error: str | None = None):
        self.cfg, self.engine, self.error = cfg, engine, error

    def describe(self) -> dict:
        return self.cfg.describe()

    def guards(self, tool: str, stage: str) -> bool:
        return self.cfg.engine != "none" and stage in self.cfg.tools.get(tool, ())

    def pre(self, tool: str, arguments: dict) -> dict | None:
        if not self.guards(tool, "pre"):
            return None
        return self._run("pre", tool, lambda: self.engine.pre(tool, arguments))

    def post(self, tool: str, arguments: dict, result) -> dict | None:
        if not self.guards(tool, "post"):
            return None
        return self._run("post", tool, lambda: self.engine.post(tool, arguments, result))

    def _run(self, stage: str, tool: str, fn) -> dict | None:
        if self.error is not None:  # a broken engine never means an unguarded tool, whatever `fail` says
            log("warn", "guardrail", tool=tool, stage=stage, verdict="error", error=self.error, ms=0)
            return self._blocked(stage, f"guardrail unavailable: {self.error}")
        t0 = time.monotonic()
        try:
            message = _with_timeout(fn, self.cfg.timeout_s)
        except Exception as e:  # noqa: BLE001 - any engine failure is a hook error, decided by `fail`
            reason = (
                f"timeout after {self.cfg.timeout_s:g}s" if isinstance(e, HookTimeout) else f"{type(e).__name__}: {e}"
            )
            ms = int((time.monotonic() - t0) * 1000)
            log("warn", "guardrail_error", tool=tool, stage=stage, fail=self.cfg.fail, error=reason, ms=ms)
            if self.cfg.fail == "open":
                return None
            return self._blocked(stage, f"guardrail unavailable: {reason}")
        ms = int((time.monotonic() - t0) * 1000)
        verdict = "allow" if message is None else "block"
        log("info", "guardrail", tool=tool, stage=stage, verdict=verdict, ms=ms)
        return None if message is None else self._blocked(stage, f"guardrail blocked: {stage}: {message}")

    def _blocked(self, stage: str, text: str) -> dict:
        return {
            "content": [{"type": "text", "text": text}],
            "isError": True,
            "_meta": {"ramen": {"guardrail": {"stage": stage, "engine": self.cfg.engine}}},
        }


def _with_timeout(fn, timeout: float):
    """Run `fn` on a daemon thread so a hung rail can neither hang the call nor hold the process open."""
    box: dict = {}

    def target():
        try:
            box["r"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
            box["e"] = e

    t = threading.Thread(target=target, daemon=True, name="ramen-guardrail")
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise HookTimeout
    if "e" in box:
        raise box["e"]
    return box.get("r")


def load(bucket: Path) -> tuple[Guard, list[dict]]:
    """The group's guard and the load errors to report as `{"package": "guardrails", "reason": ...}`."""
    path = find(bucket)
    if path is None:
        return Guard(Config()), []
    try:
        cfg = parse(path, bucket)
    except GuardrailsError as e:
        # the file could not be read: nothing is known about which tools to guard, so nothing is guarded, but the
        # group sees the error in its load result and on the group page
        log("warn", "guardrails", status="invalid", error=str(e))
        return Guard(Config()), [{"package": "guardrails", "reason": str(e)}]
    if cfg.engine == "none":
        return Guard(cfg), []
    try:
        engine = make_engine(cfg)
    except Exception as e:  # noqa: BLE001 - reported, and every opted-in tool answers "unavailable"
        reason = str(e) if isinstance(e, GuardrailsError) else f"{type(e).__name__}: {e}"
        log("warn", "guardrails", engine=cfg.engine, status="unavailable", error=reason)
        return Guard(cfg, None, reason), [{"package": "guardrails", "reason": reason}]
    log("info", "guardrails", engine=cfg.engine, fail=cfg.fail, tools=sorted(cfg.tools))
    return Guard(cfg, engine), []
