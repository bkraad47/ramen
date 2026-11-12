"""A stand-in for runtime-py that speaks the sidecar protocol of CONTRACTS §2 + §23.4/§23.5 with canned tools.

Used by node-rs/tests/modern.rs so the node's 2026-07-28 behaviour is tested without the real runtime. The manifest
hash is the content of `<bucket>/hash` (default `h1`), so a test changes it and reloads.
"""

import json
import sys
import threading
import time
from pathlib import Path

LOCK = threading.Lock()
TASKS = {}
BUCKET = Path(sys.argv[sys.argv.index("--bucket") + 1])


def out(obj):
    with LOCK:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def text(s, is_error=False):
    return {"content": [{"type": "text", "text": s}], "isError": is_error}


def obj_schema(props):
    return {"type": "object", "properties": props, "required": [], "additionalProperties": False}


def load(_params):
    hash_file = BUCKET / "hash"
    return {
        "hash": hash_file.read_text().strip() if hash_file.exists() else "h1",
        "tools": [
            {
                "name": "echo",
                "description": "says what it got",
                "inputSchema": obj_schema(
                    {
                        "region": {"type": "string", "x-mcp-header": "Region"},
                        "limit": {"type": "integer", "x-mcp-header": "Limit"},
                        "q": {"type": "string"},
                    }
                ),
            },
            {"name": "secret_tool", "description": "blocked in tests", "inputSchema": obj_schema({})},
            {
                "name": "ask",
                "description": "needs a name",
                "inputSchema": obj_schema({"mode": {"type": "string"}}),
            },
            {"name": "needs_sampling", "description": "needs sampling", "inputSchema": obj_schema({})},
            {"name": "prog", "description": "reports progress", "inputSchema": obj_schema({})},
            {
                "name": "slow",
                "description": "a task tool",
                "task": True,
                "inputSchema": obj_schema({"mode": {"type": "string"}}),
            },
        ],
        "resources": [
            {"uri": "ramen://demo/readme", "name": "readme", "description": "d", "mimeType": "text/plain"}
        ],
        "resourceTemplates": [
            {
                "uriTemplate": "ramen://items/{id}/data",
                "name": "item",
                "description": "one item",
                "mimeType": "application/json",
                "arguments": [{"name": "id", "enum": ["alpha", "alps", "beta"]}],
            },
            {"uriTemplate": "ramen://vault/{id}", "name": "vault", "description": "blocked in tests"},
        ],
        "prompts": [
            {
                "name": "greet",
                "description": "greets",
                "arguments": [
                    {"name": "tone", "description": "t", "required": True, "enum": ["warm", "wry", "cold"]},
                    {"name": "who", "description": "w", "required": True},
                ],
            },
            {"name": "ask_prompt", "description": "needs context", "arguments": []},
        ],
        "errors": [],
    }


ELICIT = {"method": "elicitation/create", "params": {"message": "Name?", "requestedSchema": {"type": "object"}}}


def call_tool(params, rid):
    name, args = params["name"], params.get("arguments", {})
    responses = params.get("inputResponses") or {}
    if name == "echo":
        seen = {k: params.get(k) for k in ("arguments", "client", "progress", "inputResponses", "state")}
        return text(json.dumps(seen, sort_keys=True))
    if name == "ask":
        if "user_name" in responses:
            who = responses["user_name"].get("content", {}).get("name")
            return text(f"Hello, {who}! state={json.dumps(params.get('state'))}")
        mode = args.get("mode", "state")
        ask = {"resultType": "input_required", "inputRequests": {"user_name": ELICIT}}
        if mode == "state":
            ask["state"] = {"round": 1}
        if mode == "url":
            url = {"mode": "url", "url": "https://example.test/x", "message": "open"}
            ask["inputRequests"] = {"go": {"method": "elicitation/create", "params": url}}
        if mode == "sampling":
            ask["inputRequests"] = {"s": {"method": "sampling/createMessage", "params": {"messages": []}}}
        return ask
    if name == "needs_sampling":
        if "sampling" in params.get("client", {}).get("capabilities", {}):
            return text("Success")
        return {"resultType": "missing_capability", "requiredCapabilities": {"sampling": {}}}
    if name == "prog":
        for i, p in enumerate((0, 50, 100)):
            line = {"id": rid, "progress": p, "total": 100}
            if i == 1:
                line["message"] = "half"
            out({"jsonrpc": "2.0", "method": "runtime.progress", "params": line})
            time.sleep(0.02)
        return text(f"done progress={json.dumps(params.get('progress'))}")
    if name == "slow":
        return text("sync:" + args.get("mode", "ok"))
    raise KeyError(name)


def run_task(tid, args):
    mode = args.get("mode", "ok")
    t = TASKS[tid]
    if mode == "input":
        t.update(status="input_required", inputRequests={"confirm": ELICIT})
        return
    if mode == "long":
        return
    time.sleep(0.2)
    if t["status"] == "cancelled":
        return
    if mode == "fail":
        t.update(status="completed", result=text("it broke", True))
    elif mode == "error":
        t.update(status="failed", error={"code": -32004, "message": "protocol trouble"})
    else:
        t.update(status="completed", result=text("task done"))


def task_start(params):
    TASKS[params["taskId"]] = {"status": "working"}
    threading.Thread(target=run_task, args=(params["taskId"], params.get("arguments", {})), daemon=True).start()
    return {"ok": True}


def task_poll(params):
    return TASKS[params["taskId"]]


def task_update(params):
    t = TASKS[params["taskId"]]
    if "confirm" in params.get("inputResponses", {}):
        t.pop("inputRequests", None)
        t.update(status="completed", result=text("confirmed"))
    return {"ok": True}


def task_cancel(params):
    t = TASKS[params["taskId"]]
    if t["status"] in ("working", "input_required"):
        t.pop("inputRequests", None)
        t["status"] = "cancelled"
    return {"ok": True}


def read_resource(params):
    uri = params["uri"]
    if uri == "ramen://demo/readme":
        return {"contents": [{"uri": uri, "mimeType": "text/plain", "text": "read me"}]}
    parts = uri.split("/")
    if uri.startswith("ramen://items/") and len(parts) == 5 and parts[4] == "data":
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps({"id": parts[3]})}]}
    raise KeyError(uri)


def get_prompt(params):
    if params["name"] == "ask_prompt":
        r = params.get("inputResponses") or {}
        if "user_context" in r:
            said = r["user_context"].get("content", {}).get("context")
            return {"messages": [{"role": "user", "content": {"type": "text", "text": f"context: {said}"}}]}
        return {"resultType": "input_required", "inputRequests": {"user_context": ELICIT}}
    if params["name"] != "greet":
        raise KeyError(params["name"])
    a = params.get("arguments", {})
    return {"messages": [{"role": "user", "content": {"type": "text", "text": f"{a.get('tone')} {a.get('who')}"}}]}


HANDLERS = {
    "runtime.load": load,
    "runtime.read_resource": read_resource,
    "runtime.get_prompt": get_prompt,
    "runtime.task_start": task_start,
    "runtime.task_poll": task_poll,
    "runtime.task_update": task_update,
    "runtime.task_cancel": task_cancel,
    "runtime.ping": lambda p: {"ok": True},
}

for raw in sys.stdin:
    if not raw.strip():
        continue
    msg = json.loads(raw)
    rid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    try:
        if method == "runtime.call_tool":
            result = call_tool(params, rid)
        elif method in HANDLERS:
            result = HANDLERS[method](params)
        else:
            out({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}})
            continue
        out({"jsonrpc": "2.0", "id": rid, "result": result})
    except KeyError as e:
        out({"jsonrpc": "2.0", "id": rid, "error": {"code": -32004, "message": f"not found: {e.args[0]}"}})
