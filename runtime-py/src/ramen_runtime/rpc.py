"""Newline-delimited JSON-RPC 2.0 server over stdin/stdout (CONTRACTS §2)."""

import json
import os
from pathlib import Path

from . import bucket as gcs
from . import deps
from .executor import Executor
from .loader import load
from .log import log

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


class Server:
    def __init__(self, bucket: Path, load_on_start: bool = False):
        self.bucket = Path(bucket)
        self.exe: Executor | None = None
        self.running = True
        if load_on_start:
            self.load({})

    def serve(self, inp, out) -> None:
        for line in inp:
            if not (line := line.strip()):
                continue
            if (resp := self.handle_line(line)) is not None:
                out.write(json.dumps(resp) + "\n")
                out.flush()
            if not self.running:
                return

    def handle_line(self, line: str) -> dict | None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            return _err(None, PARSE, f"parse error: {e}")
        rid = msg.get("id") if isinstance(msg, dict) else None
        if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
            return _err(rid, INVALID_REQ, "invalid request")
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
        self.exe = Executor(load(bucket))
        return self.exe.reg.describe() | ({"sync": summary} if summary else {})

    def _exe(self) -> Executor:
        if self.exe is None:
            raise RpcError(NOT_LOADED, "runtime.load has not succeeded yet")
        return self.exe

    def call_tool(self, params: dict) -> dict:
        name, args = _str(params, "name"), _obj(params, "arguments")
        r = self._exe().call_tool(name, args)
        log("info", "call_tool", name=name, ok=not r["isError"])
        return r

    def read_resource(self, params: dict) -> dict:
        return self._exe().read_resource(_str(params, "uri"))

    def get_prompt(self, params: dict) -> dict:
        return self._exe().get_prompt(_str(params, "name"), _obj(params, "arguments"))

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


def _err(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
