"""§24.3 / §26.3: the chart's optional collector is Jaeger or Arize Phoenix (`otel.collector: none | jaeger | phoenix`);
the Config page names whichever trace UI the chart runs."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_api import app, client, cloud, demo, login, root  # noqa: F401 - fixtures

CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "ramen"
needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def template(*sets):
    flags = [x for s in sets for x in ("--set", s)]
    r = subprocess.run(
        ["helm", "template", "ramen", str(CHART), "-n", "ramen-system", "--set", "project=p", *flags],
        capture_output=True,
        text=True,
    )
    return r.returncode, r.stderr, [d for d in yaml.safe_load_all(r.stdout) if d] if r.returncode == 0 else []


def console_env(docs):
    c = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "console")
    return {e["name"]: e.get("value") for e in c["spec"]["template"]["spec"]["containers"][0]["env"]}


@needs_helm
def test_phoenix_is_a_collector_choice_with_otlp_and_ui_on_one_port():
    rc, _, docs = template("otel.collector=phoenix")
    assert rc == 0
    dep = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "phoenix")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["image"].startswith("arizephoenix/phoenix:")
    assert {p["containerPort"] for p in c["ports"]} >= {6006, 4317}
    assert dep["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True
    svc = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "phoenix")
    assert {p["port"] for p in svc["spec"]["ports"]} == {6006, 4317}
    assert console_env(docs)["RAMEN_OTEL_UI_URL"] == "http://phoenix.ramen-system:6006"
    assert not any(d["metadata"]["name"] == "jaeger" for d in docs)


@needs_helm
def test_jaeger_and_none_are_unchanged_and_anything_else_is_refused():
    rc, _, docs = template("otel.collector=jaeger")
    assert rc == 0 and console_env(docs)["RAMEN_OTEL_UI_URL"] == "http://jaeger.ramen-system:16686"
    assert not any(d["metadata"]["name"] == "phoenix" for d in docs)
    rc, _, docs = template()
    assert rc == 0 and "RAMEN_OTEL_UI_URL" not in console_env(docs)
    rc, err, _ = template("otel.collector=tempo")
    assert rc != 0 and "none, jaeger or phoenix" in err


def test_the_config_page_names_the_trace_ui_whatever_the_chart_runs(demo, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RAMEN_OTEL_UI_URL", "http://phoenix.ramen-system:6006")
    page = demo.get("/config").text
    assert 'href="http://phoenix.ramen-system:6006"' in page and "Trace UI" in page and "Jaeger UI" not in page
