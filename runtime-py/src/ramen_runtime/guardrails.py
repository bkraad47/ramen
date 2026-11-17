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

§36.1 (1.0.0c): with engine `nemo`, `pre`/`post` may also be a list or mapping of LLM rails (`guardrails_llm.RAILS`);
the model comes from the `RAMEN_LLM_*` env (§36.2) and a rail that needs one with none set fails the load closed.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import inspect
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import guardrails_llm
from .log import log

FILES = ("guardrails.yaml", "guardrails.yml")
ENGINES = ("none", "nemo", "policy")
STAGES = ("pre", "post")
DEFAULT_TIMEOUT, MAX_TIMEOUT = 10.0, 120.0
# §36.2: LLM keys come only from Ramen; a repo `models:` still reaches the repo's own flows (since 0.7.5) until removed
MODELS_DEPRECATED = (
    "guardrails: models: in config.yml is deprecated since 1.0.0c; set the provider on the group or the org"
)
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class GuardrailsError(ValueError):
    pass


class HookTimeout(Exception):
    pass


@dataclass
class Blocked:
    """An engine's block with the rail that decided it (None for the repo's own flows / a policy.py)."""

    message: str
    rail: str | None = None


@dataclass
class Config:
    engine: str = "none"
    config: Path | None = None
    config_rel: str = ""
    fail: str = "closed"
    timeout_s: float = DEFAULT_TIMEOUT
    # every opted-in tool: a stage key present = on; its rails = the ones named there ({} = the repo's flows only)
    tools: dict[str, guardrails_llm.ToolRails] = field(default_factory=dict)
    model: dict | None = None  # the `models:` entry the engine was given (provider + model; the key stays out)

    @property
    def rails(self) -> dict[str, guardrails_llm.ToolRails]:
        """§36.1: what the nemo engine extends its config for — the tools naming a rail, the stages naming one."""
        return {
            t: guardrails_llm.ToolRails({s: r for s, r in tr.stages.items() if r})
            for t, tr in self.tools.items()
            if tr.names()
        }

    def guards(self, tool: str, stage: str) -> bool:
        return self.engine != "none" and tool in self.tools and stage in self.tools[tool].stages

    def describe(self) -> dict:
        if self.engine == "none":
            return {"engine": "none", "tools": {}}
        d = {
            "engine": self.engine,
            "fail": self.fail,
            "tools": {t: _describe(tr) for t, tr in sorted(self.tools.items())},
        }
        if self.model:
            d["llm"] = {"provider": self.model["provider"], "model": self.model["model"]}
        return d


def _describe(tr: guardrails_llm.ToolRails):
    """Static rails keep the 0.7.5 list (the group page, metrics and the harness read it); a tool naming rails gets
    `{"pre": [...], "post": [...], "llm": [...]}` (a stage key present = on; `[]` = the repo's flows only there)."""
    on = [s for s in STAGES if s in tr.stages]
    if not tr.names():
        return on
    return {s: list(tr.stages[s]) for s in on} | {"llm": tr.llm()}


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
    tools: dict[str, guardrails_llm.ToolRails] = {}
    for name, spec in (doc.get("tools") or {}).items():
        if (tr := _parse_tool(name, spec, engine, path.name)) is not None:
            tools[name] = tr
    cfg = Config(engine=engine, fail=fail, timeout_s=float(timeout), tools=tools)
    if engine != "none":
        rel = doc.get("config", "mcp/guardrails")
        if not isinstance(rel, str) or not rel:
            raise GuardrailsError(f"{path.name}: config must be a path inside the repo")
        cfg.config, cfg.config_rel = (bucket / rel).resolve(), rel
        if not cfg.config.is_relative_to(bucket.resolve()):
            raise GuardrailsError(f"{path.name}: config dir {rel!r} is outside the repo")
    return cfg


def _parse_tool(name, spec, engine: str, where: str) -> guardrails_llm.ToolRails | None:
    """One `tools.<name>` entry → its stages (None = no stage on). `true` is the 0.7.5 shorthand for `{pre: true}`."""
    if not isinstance(name, str) or not _NAME.match(name):
        raise GuardrailsError(f"{where}: tools: {name!r} is not a tool name")
    if spec is True:
        spec = {"pre": True}
    if not isinstance(spec, dict) or not all(k in STAGES for k in spec):
        raise GuardrailsError(f"{where}: tools.{name} must be {{pre: bool, post: bool}}")
    if engine != "nemo" and not all(isinstance(v, bool) for v in spec.values()):
        raise GuardrailsError(f"{where}: tools.{name} must be {{pre: bool, post: bool}} (rails need engine nemo)")
    try:
        named = {s: guardrails_llm.parse_stage(name, s, spec[s], where) for s in STAGES if s in spec}
    except guardrails_llm.RailError as e:
        raise GuardrailsError(str(e)) from None
    on = {s: named[s] or {} for s in STAGES if spec.get(s)}  # `false`, `[]` and `{}` are a stage off
    return guardrails_llm.ToolRails(on) if on else None


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

        model, rails = None, cfg.rails
        if rails:  # §36.1: the env is read only when a tool names a rail; static configs load as in 0.7.5
            try:
                model = guardrails_llm.model_from_env(os.environ)
            except guardrails_llm.RailError as e:
                raise GuardrailsError(f"guardrails: {e}") from None
            if model is None and (need := guardrails_llm.needs_model(rails)):
                raise GuardrailsError(
                    f"guardrails: rail {need[0]} on {need[1]} needs an LLM: set the group's or the org's provider"
                )
        cfg.model = model
        return guardrails_nemo.NemoEngine(cfg.config, rails, model)
    return None


def canonical(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class Guard:
    """What the executor asks before and after a tool call. `error` set = the engine did not load; `warnings` ride on
    the load result (deprecations, never a refusal)."""

    def __init__(self, cfg: Config, engine=None, error: str | None = None, warnings: list[str] | None = None):
        self.cfg, self.engine, self.error, self.warnings = cfg, engine, error, list(warnings or [])
        self._runner = _Runner()

    def describe(self) -> dict:
        return self.cfg.describe()

    def guards(self, tool: str, stage: str) -> bool:
        return self.cfg.guards(tool, stage)

    def pre(self, tool: str, arguments: dict) -> dict | None:
        tr = self.cfg.tools.get(tool)
        if self.cfg.engine == "nemo" and tr is not None and tr.llm() and guardrails_llm.judge_control(arguments):
            # §38.4 `judge_format`: before any LLM rail of the tool (pre or post), no model call
            log("info", "guardrail", tool=tool, stage="pre", verdict="block", ms=0, rail="judge_format")
            return self._blocked("pre", f"guardrail blocked: pre: {guardrails_llm.JUDGE_FORMAT}", "judge_format")
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
            message = self._runner.run(fn, self.cfg.timeout_s)
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
        rail = message.rail if isinstance(message, Blocked) else None
        if isinstance(message, Blocked):
            message = message.message
        verdict = "allow" if message is None else "block"
        log("info", "guardrail", tool=tool, stage=stage, verdict=verdict, ms=ms, **({"rail": rail} if rail else {}))
        return None if message is None else self._blocked(stage, f"guardrail blocked: {stage}: {message}", rail)

    def _blocked(self, stage: str, text: str, rail: str | None = None) -> dict:
        meta = {"stage": stage, "engine": self.cfg.engine} | ({"rail": rail} if rail else {})
        return {"content": [{"type": "text", "text": text}], "isError": True, "_meta": {"ramen": {"guardrail": meta}}}


class _Runner:
    """Hooks run on one long-lived daemon thread that owns one event loop (a NeMo rail is a coroutine on it; a
    `policy.py` hook a plain call). A thread and a loop per hook used to cost ~2.5 ms of a static rail's ~11 ms.
    A hook that outlives the timeout is abandoned with its thread — the next hook gets a fresh one — so a hung rail
    still neither hangs the call nor holds the process open (the thread is a daemon)."""

    def __init__(self):
        self.thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()  # the serving thread and a task thread may both ask for a hook

    def _start(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=loop.run_forever, daemon=True, name="ramen-guardrail")
        self.thread.start()
        self._loop = loop
        return loop

    def run(self, fn, timeout: float):
        """`fn()` on the hook thread; an awaitable it returns is awaited there. Raises what `fn` raised, or
        `HookTimeout` — after which this thread is dropped and never used again."""

        async def call():
            r = fn()
            return await r if inspect.isawaitable(r) else r

        with self._lock:
            loop = self._loop if self.thread is not None and self.thread.is_alive() else self._start()
            fut = asyncio.run_coroutine_threadsafe(call(), loop)
        try:
            return fut.result(timeout)  # outside the lock: a task's LLM rail and a call's static rail overlap
        except concurrent.futures.TimeoutError:
            fut.cancel()
            with self._lock:
                if self._loop is loop:  # the hung hook keeps the old thread; nobody waits for it
                    self.thread, self._loop = None, None
            raise HookTimeout from None


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
    warnings = [MODELS_DEPRECATED] if getattr(engine, "repo_models", False) else []
    for w in warnings:
        log("warn", "guardrails", status="deprecated", warning=w)
    return Guard(cfg, engine, warnings=warnings), []
