"""Tasks (CONTRACTS §23.5): a tool call on a worker thread, polled by the node.

A task keeps the callable it was started with, so a reload never touches it: it finishes on the code it started on.
`InputRequired` parks the task (`input_required`); `update` merges the answers for outstanding keys and runs the tool
body again with every answer so far and the last `state`. Retention: a task is dropped `KEEP_S` after it was first
polled in a terminal state, or — never polled to the end — `ttl_s + KEEP_S` after it was created, once not working.
"""

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

KEEP_S = 60.0
TERMINAL = ("completed", "failed", "cancelled")


@dataclass
class Task:
    run: Callable[[dict | None, object], dict]
    redact: Callable[[str], str]
    created: float
    status: str = "working"
    result: dict | None = None
    error: dict | None = None
    requests: dict = field(default_factory=dict)
    responses: dict = field(default_factory=dict)
    state: object = None
    polled: float | None = None
    thread: threading.Thread | None = None


class Tasks:
    def __init__(self, ttl_s: float | None = None, clock: Callable[[], float] = time.monotonic):
        self.ttl_s = float(os.environ.get("RAMEN_TASK_TTL_SECS", "3600")) if ttl_s is None else ttl_s
        self.clock = clock
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}

    def start(self, tid: str, run: Callable[[dict | None, object], dict], redact: Callable[[str], str]) -> None:
        with self._lock:
            self._purge()
            if tid in self._tasks:
                raise ValueError(f"task {tid} already exists")
            t = self._tasks[tid] = Task(run, redact, self.clock())
            self._spawn(t)

    def poll(self, tid: str) -> dict:
        with self._lock:
            self._purge()
            t = self._get(tid)
            out: dict = {"status": t.status}
            if t.status == "input_required":
                out["inputRequests"] = t.requests
            elif t.status == "completed":
                out["result"] = t.result
            elif t.status == "failed":
                out["error"] = t.error
            if t.status in TERMINAL and t.polled is None:
                t.polled = self.clock()
            return out

    def update(self, tid: str, responses: dict) -> None:
        with self._lock:
            t = self._get(tid)
            fresh = {k: v for k, v in responses.items() if k in t.requests}
            if t.status != "input_required" or not fresh:  # not outstanding: ignored (ext-tasks, tasks/update)
                return
            t.responses |= fresh
            t.status, t.requests = "working", {}
            self._spawn(t)

    def cancel(self, tid: str) -> None:
        with self._lock:
            t = self._get(tid)
            if t.status not in TERMINAL:  # cooperative: a running body ends by itself and its result is discarded
                t.status, t.requests = "cancelled", {}

    def _get(self, tid: str) -> Task:
        if tid not in self._tasks:
            raise KeyError(f"task {tid}")
        return self._tasks[tid]

    def _spawn(self, t: Task) -> None:
        t.thread = threading.Thread(target=self._run, args=(t,), daemon=True, name="ramen-task")
        t.thread.start()

    def _run(self, t: Task) -> None:
        try:
            r, err = t.run(dict(t.responses) or None, t.state), None
        except BaseException as e:  # noqa: BLE001 - a task thread must always leave a terminal status behind
            r, err = {}, {"code": -32603, "message": t.redact(f"{type(e).__name__}: {e}")}
        with self._lock:
            if t.status == "cancelled":
                return
            kind = r.get("resultType")
            if err is not None:
                t.status, t.error = "failed", err
            elif kind == "input_required":
                t.status, t.requests, t.state = "input_required", r["inputRequests"], r.get("state")
            elif kind == "missing_capability":
                data = {"requiredCapabilities": r["requiredCapabilities"]}
                t.status = "failed"
                t.error = {"code": -32021, "message": "missing required client capabilities", "data": data}
            else:
                t.status, t.result = "completed", r

    def _purge(self) -> None:
        now = self.clock()
        for tid, t in list(self._tasks.items()):
            polled_out = t.polled is not None and now - t.polled > KEEP_S
            if polled_out or (t.status != "working" and now - t.created > self.ttl_s + KEEP_S):
                del self._tasks[tid]
