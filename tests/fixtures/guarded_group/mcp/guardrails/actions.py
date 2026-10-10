"""Deterministic rails for the conformance fixture: no model, two substring checks."""

from nemoguardrails.actions import action


@action(is_system_action=True)
async def check_blocked_words(context: dict | None = None) -> bool:
    return "drop table" in ((context or {}).get("user_message") or "").lower()


@action(is_system_action=True)
async def check_output_words(context: dict | None = None) -> bool:
    return "secret" in ((context or {}).get("bot_message") or "").lower()
