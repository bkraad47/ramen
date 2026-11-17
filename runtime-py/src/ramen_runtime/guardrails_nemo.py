"""Engine `nemo` (CONTRACTS §21.2, §36.1): NeMo Guardrails rails run in this process; an LLM is called only for the
rails a tool names in `guardrails.yaml`.

The config dir is a standard NeMo rails config (`config.yml`, `*.co` flows, `actions.py`, optionally `prompts.yml`).
Only the rails are run: `pre` runs the **input** rails over the tool name and the redacted arguments, `post` runs the
**output** rails over the result. A rail that stops the flow blocks the call with its bot message. Actions see the
tool as the `tool` context variable. Named LLM rails become generated wrapper flows gated by the call's `$ramen_rails`
(`guardrails_llm`), appended to the repo's own flows; `models:` and the missing prompts are set on the parsed config
object only. A tool that names no rail at a stage runs on a second `LLMRails` built from the repo config alone (see
`__init__`). `pre`/`post` return coroutines, awaited on the hook thread (`guardrails._Runner`). The `[nemo]` extra
installs `nemoguardrails`; the runtime is pinned to Python 3.12 for it (D45).
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from pathlib import Path

from . import guardrails_llm
from .guardrails import Blocked, GuardrailsError, canonical

INTERNAL_ERROR = "I'm sorry, an internal error has occurred."


class NemoEngine:
    name = "nemo"

    def __init__(
        self, config: Path, rails: Mapping[str, guardrails_llm.ToolRails] | None = None, model: dict | None = None
    ):
        try:
            from nemoguardrails import LLMRails, RailsConfig
        except ImportError:
            raise GuardrailsError("engine nemo is not installed (pip install 'ramen-runtime[nemo]')") from None
        self.tools = dict(rails or {})
        key = (model or {}).get("parameters", {}).get("api_key")
        self.secret = key if isinstance(key, str) and key else None  # a `Token` is fetched per request, never logged
        try:
            # Colang 1.0 runs every configured input (output) rail flow on every call, a named rail's wrapper flow
            # included (it then only finds its name absent from `$ramen_rails`): on the demo that made a static-only
            # tool's pre rail cost ~3x the repo's own flows. So a tool with no rail named at a stage runs on `static`,
            # the repo config alone (the 0.7.5 engine); `rails` carries the wrappers. One object when nothing is named.
            repo = RailsConfig.from_path(str(config))
            self.repo_models = bool(repo.models)  # deprecated since 1.0.0c (§36.2): `guardrails.load` warns
            self.static = LLMRails(repo)
            self.rails = self.static
            if self.tools:
                cfg = RailsConfig.from_path(str(config))
                self._extend(cfg, model)
                self.rails = LLMRails(cfg)
                # the post rails' prompts read the tool's own data (§36.1 fix: evidence = result + args)
                self.rails.register_prompt_context("ramen_evidence", lambda: guardrails_llm.EVIDENCE.get(""))
                self.rails.register_output_parser(guardrails_llm.final_answer, guardrails_llm.FINAL_ANSWER)
                for name, fn in guardrails_llm.FILTERS.items():  # §38.4: payloads reach a judge as data blocks
                    self.rails.register_filter(fn, name)
        except GuardrailsError:
            raise
        except Exception as e:  # noqa: BLE001 - a config problem is reported as the load error
            raise GuardrailsError(f"engine nemo: {type(e).__name__}: {self._scrub(str(e))}") from None

    def _extend(self, cfg, model: dict | None) -> None:
        """§36.1: wrapper flows, prompts and `models:` for the named rails, on the config object (never on disk)."""
        import importlib.util

        from nemoguardrails.colang import parse_colang_file
        from nemoguardrails.rails.llm.config import Model, TaskPrompt

        if any("sensitive_data" in tr.names() for tr in self.tools.values()) and not importlib.util.find_spec(
            "presidio_analyzer"
        ):
            tool = next(t for t, tr in self.tools.items() if "sensitive_data" in tr.names())
            raise GuardrailsError(
                f"guardrails: rail sensitive_data on {tool} needs presidio (pip install 'nemoguardrails[sdd]')"
            )
        if str(cfg.colang_version) != "1.0":
            raise GuardrailsError("guardrails: named rails need a Colang 1.0 config (colang_version: 1.0)")
        text, flows = guardrails_llm.colang(self.tools)
        parsed = parse_colang_file("ramen_rails.co", content=text, version="1.0")
        cfg.flows.extend(parsed["flows"])
        for message_id, utterances in parsed.get("bot_messages", {}).items():
            cfg.bot_messages.setdefault(message_id, utterances)
        cfg.rails.input.flows.extend(flows["input"])
        cfg.rails.output.flows.extend(flows["output"])
        present = {p.task for p in cfg.prompts or []}
        cfg.prompts = list(cfg.prompts or []) + [TaskPrompt(**p) for p in guardrails_llm.prompts(self.tools, present)]
        for tr in self.tools.values():
            for stage, named in tr.stages.items():
                if (e := named.get("sensitive_data", {}).get("entities")) is not None:
                    getattr(
                        cfg.rails.config.sensitive_data_detection, "input" if stage == "pre" else "output"
                    ).entities = e
        cfg.lowest_temperature = 0.0  # every judge call is deterministic; only self_consistency samples at 1.0
        if model is not None and guardrails_llm.needs_model(self.tools):
            engine = model["engine"]
            if model.get("tls_ca") and engine != "azure":
                engine = tls_engine(engine, guardrails_llm.CaFile(model["tls_ca"]))
            cfg.models = [m for m in cfg.models if m.type != "main"] + [
                Model(type="main", engine=engine, model=model["model"], parameters=dict(model["parameters"]))
            ]

    def _scrub(self, text: str) -> str:
        return text.replace(self.secret, "***") if self.secret else text

    def rails_for(self, tool: str, stage: str):
        """The engine a tool's stage runs on: the extended one when the tool names a rail there, else the repo's."""
        tr = self.tools.get(tool)
        return self.rails if tr is not None and tr.stages.get(stage) else self.static

    async def _run(self, which: str, tool: str, messages: list[dict], prompt: str, result=None) -> Blocked | None:
        """A coroutine: `Guard` awaits it on the hook thread's loop (`guardrails._Runner`)."""
        stage = "pre" if which == "input" else "post"
        ctx = guardrails_llm.context(tool, self.tools.get(tool), stage, prompt, result)
        guardrails_llm.EVIDENCE.set(ctx.get("ramen_evidence", ""))  # this task's context; NeMo's prompt reads it
        try:
            r = await self.rails_for(tool, stage).generate_async(
                messages=[{"role": "context", "content": ctx}, *messages],
                options={"rails": [which], "log": {"activated_rails": True}},
            )
        except Exception as e:  # noqa: BLE001 - a hook error, decided by `fail`; the key never reaches a log line
            raise RuntimeError(f"engine nemo: {type(e).__name__}: {self._scrub(str(e))}") from None
        activated = r.log.activated_rails if r.log else []
        stopped = next((a for a in activated if getattr(a, "stop", False)), None)
        # messages in → a list of messages out; a flow that stops without a bot message answers "" (the default below)
        resp = next((m.get("content") for m in reversed(r.response) if m.get("role") == "assistant"), None)
        if stopped is None:
            if resp == INTERNAL_ERROR:  # an action raised (model unreachable, bad key): never an open door
                raise RuntimeError("engine nemo: a rail's action failed (see the runtime log)")
            return None
        return Blocked(str(resp or "blocked by a rail"), guardrails_llm.rail_of(stopped.name))

    def pre(self, tool: str, arguments: dict) -> Awaitable[Blocked | None]:
        args = canonical(arguments)
        return self._run("input", tool, [{"role": "user", "name": tool, "content": args}], args)

    def post(self, tool: str, arguments: dict, result) -> Awaitable[Blocked | None]:
        args = canonical(arguments)
        return self._run(
            "output",
            tool,
            [{"role": "user", "name": tool, "content": args}, {"role": "assistant", "content": canonical(result)}],
            args,
            result,
        )


class CaTransport:
    """An httpx transport over `ca.ctx` that re-reads the CA file before each request and, when it changed, moves to a
    new pooled transport on the new context (§39.1 rotation without a restart; the old pool is closed)."""

    def __init__(self, ca, limits):
        import httpx

        self.ca, self.limits, self.ctx = ca, limits, ca.ctx
        self.inner = httpx.AsyncHTTPTransport(verify=ca.ctx, limits=limits)

    async def handle_async_request(self, request):
        import httpx

        self.ca.refresh()
        if self.ca.ctx is not self.ctx:
            old, self.ctx = self.inner, self.ca.ctx
            self.inner = httpx.AsyncHTTPTransport(verify=self.ctx, limits=self.limits)
            await old.aclose()
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()


def tls_engine(engine: str, ca) -> str:
    """D86 (§39.3): NeMo's default framework builds its own httpx client and takes no `verify`; register a provider
    `ramen_tls_<engine>` that is the same OpenAI-compatible client over an httpx client trusting `ca.ctx` (the system
    roots plus the install CA), and name it as the model's engine. Nothing process-wide changes. §39.1: the bundle
    file is re-read before each request (`CaFile.refresh`), so a rotated install CA is trusted without a restart."""
    import httpx
    from nemoguardrails.llm.clients.constants import DEFAULT_CONNECTION_LIMITS, DEFAULT_TIMEOUT
    from nemoguardrails.llm.clients.openai_compatible import OpenAICompatibleClient
    from nemoguardrails.llm.frameworks.registry import get_framework
    from nemoguardrails.llm.models.openai_chat import OpenAIChatModel

    names = {"timeout": "timeout", "connect_timeout": "connect_timeout", "max_retries": "max_retries",
             "default_headers": "custom_headers", "default_query": "custom_query"}  # fmt: skip

    def make(model: str, **kw):
        client_kw = {out: kw.pop(name) for name, out in names.items() if kw.get(name) is not None}
        timeout = httpx.Timeout(client_kw.get("timeout", DEFAULT_TIMEOUT.read), connect=DEFAULT_TIMEOUT.connect)

        http = httpx.AsyncClient(transport=CaTransport(ca, DEFAULT_CONNECTION_LIMITS), timeout=timeout)
        client = OpenAICompatibleClient(
            base_url=kw.pop("base_url"), api_key=kw.pop("api_key", None), http_client=http, **client_kw
        )
        return OpenAIChatModel(client=client, model=model, provider_name=engine, **kw)

    name = f"ramen_tls_{engine}"
    get_framework("default").register_provider(name, make)
    return name
