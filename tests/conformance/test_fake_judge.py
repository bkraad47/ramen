"""The CI judge (CONTRACTS §36.4, §39): `ramen_tests.fakes.FakeOpenAI` served over TLS with a leaf of a generated CA,
so CI deploys the real demo repo (LLM rails on `iris_report` / `iris_outliers`) with no key and no plaintext hop.
Compose runs it as service `fake-llm` (profile `ci`, leaf from the `certs` service); the conformance harness starts one
per process (`localconsole.fake_judge`) and every `LocalConsole` trusts its CA. Static checks need nothing; the
served cases need `openssl`; the console case needs the console venv."""

import json
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml

from ramen_tests import env as E
from ramen_tests.fakes import FAKE_JUDGE_KEY, FAKE_MODEL, JAILBREAK_MARKER, FakeOpenAI, judge_provider
from ramen_tests.localnode import free_port, self_signed

pytestmark = pytest.mark.conformance
COMPOSE = E.RAMEN_DIR / "deploy" / "local" / "docker-compose.yml"
CI = E.RAMEN_DIR / ".github" / "workflows" / "ci.yml"
FAKES = E.RAMEN_DIR / "tests" / "src" / "ramen_tests" / "fakes.py"
# the tails of the runtime's judge prompts (runtime-py/src/ramen_runtime/guardrails_llm.py)
BLOCK_QUESTION = "Question: Should the tool arguments be blocked (Yes or No)?\nAnswer:"
HALLUCINATION = 'Then end with a final line that is exactly "Answer: Yes" if any sentence is unsupported'
ENTAILS = 'Report to check:\n<<<DATA\n4 rows\nDATA>>>\n"entails":'


def trust(ca: str) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=ca)


def ask(url: str, ca: str, prompt: str) -> str:
    body = {"model": FAKE_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0}
    auth = {"Authorization": "Bearer x"}
    r = httpx.post(f"{url}/chat/completions", json=body, verify=trust(ca), headers=auth, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["choices"][0]["message"]["content"]


@pytest.fixture
def served(tmp_path):
    cert, key = self_signed(tmp_path)
    with FakeOpenAI("judge", cert=str(cert), key=str(key)) as fake:
        yield fake, str(cert)


def test_the_judge_serves_tls_and_lets_every_normal_call_through(served):
    fake, ca = served
    assert fake.url.startswith("https://127.0.0.1:") and fake.url.endswith("/v1"), fake.url
    models = httpx.get(f"{fake.url}/models", verify=trust(ca), timeout=10).json()
    assert FAKE_MODEL in {m["id"] for m in models["data"]}
    assert ask(fake.url, ca, f"Tool arguments:\n<<<ARGS\n{{}}\nARGS>>>\n\n{BLOCK_QUESTION}") == "No"
    assert ask(fake.url, ca, HALLUCINATION).splitlines()[-1] in ("No", "Answer: No"), "hallucination: supported"
    assert ask(fake.url, ca, ENTAILS).lower() == "yes", "fact_checking asks the other way round: entailed = yes"
    assert ask(fake.url, ca, f"{JAILBREAK_MARKER}\n{BLOCK_QUESTION}") == "Yes", "the marker still blocks"
    assert ask(fake.url, ca, f"{JAILBREAK_MARKER}\n{ENTAILS}").lower() == "no"
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"{fake.url}/models", timeout=5)  # the system roots do not know the leaf: TLS, verified


def test_the_judge_refuses_plaintext(served):
    fake, _ = served
    with pytest.raises(httpx.HTTPError):
        httpx.get(fake.url.replace("https://", "http://") + "/models", timeout=5)


def test_the_judge_runs_as_a_script_the_way_compose_starts_it(tmp_path):
    cert, key = self_signed(tmp_path)
    port = free_port()
    args = ["--listen", f"127.0.0.1:{port}", "--cert", str(cert), "--key", str(key)]
    proc = subprocess.Popen([sys.executable, str(FAKES), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)  # noqa: S603
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        url = f"https://127.0.0.1:{port}/v1"
        assert ask(url, str(cert), BLOCK_QUESTION) == "No"
    finally:
        proc.terminate()
        proc.wait(10)


def test_judge_provider_is_the_org_card_for_the_fake():
    body = judge_provider("https://fake-llm:8443/v1")
    assert body == {
        "openai": {"api_key": FAKE_JUDGE_KEY, "base_url": "https://fake-llm:8443/v1", "allowed_models": [FAKE_MODEL]}
    }
    with pytest.raises(ValueError, match="https"):
        judge_provider("http://fake-llm:8443/v1")


def test_compose_runs_the_judge_under_the_ci_profile_only():
    doc = yaml.safe_load(COMPOSE.read_text())
    svc = doc["services"]["fake-llm"]
    assert svc["profiles"] == ["ci"], "a plain `make up` never starts it"
    assert "ports" not in svc, "reached by the console on the compose network only"
    flat = " ".join(svc["entrypoint"])
    assert "/certs/fake-llm/tls.crt" in flat and "/certs/fake-llm/tls.key" in flat and "--listen" in flat
    leaves = doc["services"]["certs"]["command"]
    assert any(x.startswith("fake-llm=fake-llm,") for x in leaves), leaves
    console = doc["services"]["console"]["environment"]
    assert console["RAMEN_CONSOLE_INTERNAL_URL"] == "https://console:8443", "workers reach the judge proxy by name"


def test_ci_starts_the_judge_and_hands_the_harness_its_https_url():
    jobs = yaml.safe_load(CI.read_text())["jobs"]
    e2e = jobs["e2e"]
    assert e2e["env"]["RAMEN_FAKE_LLM_URL"].startswith("https://fake-llm:"), e2e["env"]
    runs = " ".join(s.get("run", "") for s in e2e["steps"])
    assert "--profile ci up" in runs and "--profile ci down" in runs, runs
    node = " ".join(s.get("run", "") for s in jobs["node-conformance"]["steps"])
    assert "conformance/test_fake_judge.py" in node


def test_a_local_console_trusts_the_judge_and_deploys_the_demo_with_it(tmp_path):
    from ramen_tests.localconsole import LocalConsole

    with LocalConsole() as con:
        con.seed_group()
        a = con.admin
        listed = a.get("config_llm_models", provider="openai").json()
        assert listed["source"] == "live" and FAKE_MODEL in listed["models"], listed
        envs = [e for e in a.get("environments_all", params={"group": "demo"}).json() if e["name"] == "dev"]
        log = json.dumps(envs[0].get("last_deploy") or {})
        assert "no LLM provider" not in log, log
        env_file = Path(con.dir) / "buckets" / "demo" / ".ramen" / "env-local"
        lines = env_file.read_text().splitlines() if env_file.exists() else []
        if any(x.startswith("RAMEN_GUARDRAILS_VERSION=") for x in lines):  # the synced repo names LLM rails
            assert f"RAMEN_LLM_MODEL={FAKE_MODEL}" in lines, lines
