"""Engine `nemo` (CONTRACTS §21.2): NeMo Guardrails rails run in this process, no LLM call unless the config asks.

The config dir is a standard NeMo rails config (`config.yml`, `*.co` flows, `actions.py`). Only the rails are run:
`pre` runs the **input** rails over the tool name and the redacted arguments, `post` runs the **output** rails over the
result. A rail that stops the flow blocks the call with its bot message. Actions see the tool as the `tool` context
variable. The `[nemo]` extra installs `nemoguardrails`; the runtime is pinned to Python 3.12 for it (D45).
"""

from __future__ import annotations

from pathlib import Path

from .guardrails import GuardrailsError, canonical


class NemoEngine:
    name = "nemo"

    def __init__(self, config: Path):
        try:
            from nemoguardrails import LLMRails, RailsConfig
        except ImportError:
            raise GuardrailsError("engine nemo is not installed (pip install 'ramen-runtime[nemo]')") from None
        try:
            self.rails = LLMRails(RailsConfig.from_path(str(config)))
        except Exception as e:  # noqa: BLE001 - a config problem is reported as the load error
            raise GuardrailsError(f"engine nemo: {type(e).__name__}: {e}") from None

    def _run(self, which: str, tool: str, messages: list[dict]) -> str | None:
        r = self.rails.generate(
            messages=[{"role": "context", "content": {"tool": tool}}, *messages],
            options={"rails": [which], "log": {"activated_rails": True}},
        )
        stopped = any(getattr(a, "stop", False) for a in (r.log.activated_rails if r.log else []))
        if not stopped:
            return None
        resp = r.response
        if isinstance(resp, list):
            resp = next((m.get("content") for m in reversed(resp) if m.get("role") == "assistant"), None)
        return str(resp or "blocked by a rail")

    def pre(self, tool: str, arguments: dict) -> str | None:
        return self._run("input", tool, [{"role": "user", "name": tool, "content": canonical(arguments)}])

    def post(self, tool: str, arguments: dict, result) -> str | None:
        return self._run(
            "output",
            tool,
            [
                {"role": "user", "name": tool, "content": canonical(arguments)},
                {"role": "assistant", "content": canonical(result)},
            ],
        )
