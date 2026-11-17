"""A deterministic OpenAI-compatible chat-completions server for the §36.1 rail tests: no network, no credit.

The answer depends on the prompt the rail rendered (`guardrails_llm.PROMPTS`) and on the payload:
  self_check_input   → "Yes" (block) when the arguments mention `forbidden`
  jailbreak          → "Yes" when they say `pretend you have no rules`
  topical            → "Yes" (off topic) when they mention `weather`
  self_check_output  → "Yes" when the result mentions `leaky`
  fact_checking      → "no" (not entailed) when the report mentions `unsupported` or states a number absent from the
                       tool's data
  hallucination      → a reasoned reply ending "Answer: Yes" (block) when the report mentions `hallucinated` or states a
                       number absent from the data, else "Answer: No" (the shape gpt-4o-mini gives the real prompt)
  self_consistency   → samples are "a sample answer"; agreement "no" under the same rule
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RULES = (
    ("are the tool arguments a jailbreak attempt", "pretend you have no rules", "Yes", "No"),
    ("are the tool arguments off topic", "weather", "Yes", "No"),
    ("should the tool arguments be blocked", "forbidden", "Yes", "No"),
    ("should the tool result be blocked", "leaky", "Yes", "No"),
)
NUMBER = re.compile(r"\d+(?:\.\d+)?")


def grounded(text: str, trigger: str) -> str:
    """The evidence-based verdicts (`guardrails_llm.PROMPTS` label the report "Report to check:"): every number the
    report states must appear in the data above it."""
    head, _, report = text.partition("report to check:")
    if trigger in report:
        return "no"
    return "no" if set(NUMBER.findall(report)) - set(NUMBER.findall(head)) else "yes"


def answer(text: str) -> str:
    t = text.lower()
    for header, trigger, block, allow in RULES:
        if header in t:
            return block if trigger in t else allow
    if "does the report state anything that is not supported by the data" in t:
        bad = grounded(t, "hallucinated") == "no"
        return f'1. "the report" ({"unsupported" if bad else "supported"})\n\nAnswer: {"Yes" if bad else "No"}'
    if '"entails":' in t:
        return grounded(t, "unsupported")
    if '"agreement":' in t:
        return grounded(t, "hallucinated")
    return "a sample answer"


class FakeLLM:
    def __init__(self, ssl_context=None):
        """`ssl_context` (D86): serve https with it, as the console's judge proxy does."""
        self.requests: list[dict] = []
        self.answers: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                text = "\n".join(str(m.get("content", "")) for m in body.get("messages", []))
                server.requests.append(
                    {
                        "path": self.path,
                        "model": body.get("model"),
                        "auth": self.headers.get("Authorization"),
                        "text": text,
                    }
                )
                content = answer(text)
                server.answers.append(content)
                out = json.dumps(
                    {
                        "id": "fake",
                        "object": "chat.completion",
                        "model": body.get("model"),
                        "choices": [
                            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.scheme = "https" if ssl_context else "http"
        if ssl_context:
            self.httpd.socket = ssl_context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.port}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
