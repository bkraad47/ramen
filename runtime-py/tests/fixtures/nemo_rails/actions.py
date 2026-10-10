from nemoguardrails.actions import action


@action(is_system_action=True)
async def looks_like_injection(context: dict | None = None):
    ctx = context or {}
    msg = (ctx.get("user_message") or "").lower()
    # the tool name is a context variable: rails can be tool-specific
    return "ignore previous instructions" in msg and ctx.get("tool") != "unguarded_tool"


@action(is_system_action=True)
async def leaks_secret(context: dict | None = None):
    return "secret" in ((context or {}).get("bot_message") or "").lower()
