"""Newline-delimited JSON-RPC 2.0 server over stdin/stdout (CONTRACTS §2).

D83 (1.0.0c): requests run on a pool of `RAMEN_RUNTIME_CONCURRENCY` threads (default 4), so a slow tool or an LLM rail
no longer holds every other call on the worker; the node matches answers by id. `runtime.load` and `runtime.shutdown`
are exclusive: they wait for the calls in flight (up to the guardrail timeout) and hold new ones back meanwhile. Tenant
code may therefore run on several threads at once (wiki: mcp-repo.md).
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import bucket as gcs
from . import deps, envfile, guardrails, secrets
from .executor import Executor
from .loader import load
from .log import log
from .tasks import Tasks

PARSE, INVALID_REQ, NOT_FOUND, INVALID_PARAMS, INTERNAL, NOT_LOADED, PKG_NOT_FOUND = (
    -32700,
    -32600,
    -32601,
    -32602,
    -32603,
    -32002,
    -32004,
)


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


EXCLUSIVE = ("runtime.load", "runtime.shutdown")


def concurrency() -> int:
    """`RAMEN_RUNTIME_CONCURRENCY`: calls run at once, default 4, at least 1 (a value that is not a number: 4)."""
    try:
        return max(1, int(os.environ.get("RAMEN_RUNTIME_CONCURRENCY", "4")))
    except ValueError:
        return 4


class _RWLock:
    """Calls hold it shared; a reload holds it exclusively. One writer at a time (the serving thread), who waits for
    the readers up to `timeout` seconds and then goes ahead anyway (a call stuck for longer keeps its old executor)."""

    def __init__(self):
        self._cv = threading.Condition()
        self._readers = 0
        self._writer = False

    def acquire_shared(self) -> None:
        with self._cv:
            while self._writer:
                self._cv.wait()
            self._readers += 1

    def release_shared(self) -> None:
        with self._cv:
            self._readers -= 1
            self._cv.notify_all()

    def acquire_exclusive(self, timeout: float) -> None:
        with self._cv:
            self._writer = True  # from here on new calls wait
            deadline = time.monotonic() + timeout
            while self._readers and (left := deadline - time.monotonic()) > 0:
                self._cv.wait(left)

    def release_exclusive(self) -> None:
        with self._cv:
            self._writer = False
            self._cv.notify_all()


class Server:
    def __init__(self, bucket: Path, load_on_start: bool = False):
        self.bucket = Path(bucket)
        self.exe: Executor | None = None
        self.running = True
        self.tasks = Tasks()  # §23.5: outlives a reload; each task keeps the executor it started on
        self.concurrency = concurrency()
        self._out = None
        self._local = threading.local()  # `.rid`: the id of the request this thread serves, for `runtime.progress`
        self._lock = threading.Lock()  # one line at a time on stdout
        self._calls = _RWLock()
        if load_on_start:
            self.load({})

    def serve(self, inp, out) -> None:
        self._out = out
        pool = ThreadPoolExecutor(self.concurrency, thread_name_prefix="ramen-call")
        try:
            for line in inp:
                if not (line := line.strip()):
                    continue
                msg = self._parse(line)
                if isinstance(msg, dict) and msg.get("method") in EXCLUSIVE:
                    self._calls.acquire_exclusive(self._drain_timeout())
                    try:
                        self._answer(msg)
                    finally:
                        self._calls.release_exclusive()
                    if not self.running:
                        return
                else:
                    # taken here, not on the pool thread: a call read before a reload is served before it
                    self._calls.acquire_shared()
                    pool.submit(self._shared, msg)
        finally:
            pool.shutdown(wait=True)  # EOF: the calls in flight still answer

    def _shared(self, msg) -> None:
        try:
            self._answer(msg)
        finally:
            self._calls.release_shared()

    def _answer(self, msg) -> None:
        if (resp := self.handle(msg)) is not None:
            self._write(resp)

    def _drain_timeout(self) -> float:
        return self.exe.guard.cfg.timeout_s if self.exe is not None else guardrails.DEFAULT_TIMEOUT

    def _write(self, msg: dict) -> None:
        with self._lock:
            self._out.write(json.dumps(msg) + "\n")
            self._out.flush()

    @staticmethod
    def _parse(line: str):
        """The message, or the parse-error answer for it (handed on like a message: `handle` passes it through)."""
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            return _err(None, PARSE, f"parse error: {e}")

    def handle_line(self, line: str) -> dict | None:
        return self.handle(self._parse(line))

    def handle(self, msg) -> dict | None:
        if isinstance(msg, dict) and "error" in msg and "method" not in msg:
            return msg  # a parse error `_parse` already shaped
        rid = msg.get("id") if isinstance(msg, dict) else None
        if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
            return _err(rid, INVALID_REQ, "invalid request")
        self._local.rid = rid
        try:
            result = self.dispatch(msg["method"], msg.get("params") or {})
        except RpcError as e:
            log("warn", "rpc error", method=msg["method"], code=e.code, error=str(e))
            return None if rid is None else _err(rid, e.code, str(e))
        return None if rid is None else {"jsonrpc": "2.0", "id": rid, "result": result}

    def dispatch(self, method: str, params: dict) -> dict:
        handler = {
            "runtime.load": self.load,
            "runtime.call_tool": self.call_tool,
            "runtime.read_resource": self.read_resource,
            "runtime.get_prompt": self.get_prompt,
            "runtime.task_start": self.task_start,
            "runtime.task_poll": self.task_poll,
            "runtime.task_update": self.task_update,
            "runtime.task_cancel": self.task_cancel,
            "runtime.ping": lambda p: {"ok": True},
            "runtime.shutdown": self.shutdown,
        }.get(method)
        if handler is None:
            raise RpcError(NOT_FOUND, f"method not found: {method}")
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        try:
            return handler(params)
        except RpcError:
            raise
        except KeyError as e:
            raise RpcError(PKG_NOT_FOUND, f"not found: {e.args[0]}") from None
        except ValueError as e:
            raise RpcError(INVALID_PARAMS, str(e)) from None
        except Exception as e:  # noqa: BLE001
            raise RpcError(INTERNAL, f"{type(e).__name__}: {e}") from None

    def load(self, params: dict) -> dict:
        bucket = Path(params.get("bucket") or self.bucket)
        summary = gcs.sync(uri, bucket) if (uri := os.environ.get("RAMEN_BUCKET_URI")) else None
        deps.install(bucket)
        env = envfile.apply(bucket)  # 0.6.0: mcp/env.yaml rendered from secrets into this process's environment
        reg = load(bucket)
        # §21.2: after env.yaml, so a repo's rail code reading its own env at load sees it; LLM keys come from Ramen
        # (`RAMEN_LLM_*`, §36.2) — env.yaml keys for rails are deprecated since 1.0.0c
        guard, errors = guardrails.load(bucket)
        reg.errors.extend(errors)
        self.exe = Executor(reg, guard)
        # `guardrails` rides next to the manifest and is not part of its hash (C1)
        return (
            self.exe.reg.describe()
            | {"env": sorted(env), "guardrails": guard.describe()}
            | ({"warnings": guard.warnings} if guard.warnings else {})
            | ({"sync": summary} if summary else {})
        )

    def _exe(self) -> Executor:
        if self.exe is None:
            raise RpcError(NOT_LOADED, "runtime.load has not succeeded yet")
        return self.exe

    def call_tool(self, params: dict) -> dict:
        name, args = _str(params, "name"), _obj(params, "arguments")
        r = self._exe().call_tool(name, args, **self._round(params))
        log("info", "call_tool", name=name, ok=not r.get("isError", False), **_kind(r))
        return r

    def _round(self, params: dict) -> dict:
        """§23.4: what a call carries besides name and arguments."""
        return {
            "responses": _responses(params),
            "state": params.get("state"),
            "client": _capabilities(params),
            "sink": self._sink() if params.get("progress") is True else None,
        }

    def _sink(self):
        """Writes `runtime.progress` lines tagged with the id of the request being served (§23.4)."""
        rid = getattr(self._local, "rid", None)
        if rid is None or self._out is None:
            return None
        return lambda p: self._write({"jsonrpc": "2.0", "method": "runtime.progress", "params": {"id": rid} | p})

    def read_resource(self, params: dict) -> dict:
        return self._exe().read_resource(_str(params, "uri"))

    def get_prompt(self, params: dict) -> dict:
        return self._exe().get_prompt(_str(params, "name"), _obj(params, "arguments"), **self._round(params))

    def task_start(self, params: dict) -> dict:
        """§23.5: returns at once; the tool runs on a thread, every round through `Executor.call_tool` (so pre runs
        before the start and each re-run, post on the final result). No progress lines for a task."""
        tid, name, args = _str(params, "taskId"), _str(params, "name"), _obj(params, "arguments")
        exe, caps = self._exe(), _capabilities(params)
        exe.tool(name)  # unknown tool → PKG_NOT_FOUND now, not a failed task later

        def redact(text: str) -> str:
            used: list[str] = []
            try:
                secrets.substitute_args(args, used)
            except secrets.SecretError:
                pass
            return secrets.redact(text, used + envfile.rendered_values())

        self.tasks.start(
            tid,
            lambda responses, state: exe.call_tool(name, args, responses=responses, state=state, client=caps),
            redact,
        )
        log("info", "task_start", name=name, task=tid)
        return {"status": "working"}

    def task_poll(self, params: dict) -> dict:
        return self.tasks.poll(_str(params, "taskId"))

    def task_update(self, params: dict) -> dict:
        self.tasks.update(_str(params, "taskId"), _responses(params) or {})
        return {"ok": True}

    def task_cancel(self, params: dict) -> dict:
        self.tasks.cancel(_str(params, "taskId"))
        return {"ok": True}

    def shutdown(self, params: dict) -> dict:
        self.running = False
        return {"ok": True}


def _str(params: dict, key: str) -> str:
    if not isinstance(v := params.get(key), str):
        raise RpcError(INVALID_PARAMS, f"{key} must be a string")
    return v


def _obj(params: dict, key: str) -> dict:
    if not isinstance(v := params.get(key, {}), dict):
        raise RpcError(INVALID_PARAMS, f"{key} must be an object")
    return v


def _responses(params: dict) -> dict | None:
    v = params.get("inputResponses")
    if v is None:
        return None
    if not isinstance(v, dict):
        raise RpcError(INVALID_PARAMS, "inputResponses must be an object")
    for k, r in v.items():
        if not isinstance(r, dict):
            raise RpcError(INVALID_PARAMS, f"inputResponses.{k} must be an object")
    return v


def _capabilities(params: dict) -> dict:
    caps = _obj(params, "client").get("capabilities", {})
    if not isinstance(caps, dict):
        raise RpcError(INVALID_PARAMS, "client.capabilities must be an object")
    return caps


def _kind(r: dict) -> dict:
    return {"result": r["resultType"]} if "resultType" in r else {}


def _err(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
