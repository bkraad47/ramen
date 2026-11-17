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
    by = {c["name"]: c for c in dep["spec"]["template"]["spec"]["containers"]}
    assert by["phoenix"]["image"].startswith("arizephoenix/phoenix:")
    assert {p["containerPort"] for p in by["phoenix"]["ports"]} >= {6006, 4317}
    env = {e["name"]: e["value"] for e in by["phoenix"]["env"]}
    assert env["PHOENIX_ENABLE_AUTH"] == "false"  # a non-loopback bind would switch auth on and 401 every OTLP post
    assert dep["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"] is True
    # Phoenix takes protobuf only (415 on Ramen's OTLP/JSON) and has no /v1/metrics (405): a collector sidecar on
    # 4318 converts the traces and drops the metrics, so the endpoint is the same port as the chart's Jaeger
    assert by["collector"]["image"].startswith("otel/opentelemetry-collector:")
    assert {p["containerPort"] for p in by["collector"]["ports"]} == {4318}
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "phoenix-collector")
    cfg = yaml.safe_load(cm["data"]["config.yaml"])
    assert cfg["exporters"]["otlp_http"]["endpoint"] == "https://127.0.0.1:6006"  # D86
    assert cfg["service"]["pipelines"]["traces"]["exporters"] == ["otlp_http"]
    assert cfg["service"]["pipelines"]["metrics"]["exporters"] == ["nop"]
    svc = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "phoenix")
    assert {p["port"] for p in svc["spec"]["ports"]} == {4318, 6006, 4317}
    assert console_env(docs)["RAMEN_OTEL_UI_URL"] == "https://phoenix.ramen-system:6006"
    assert not any(d["metadata"]["name"] == "jaeger" for d in docs)


@needs_helm
def test_jaeger_and_none_are_unchanged_and_anything_else_is_refused():
    rc, _, docs = template("otel.collector=jaeger")
    assert rc == 0 and console_env(docs)["RAMEN_OTEL_UI_URL"] == "https://jaeger.ramen-system:16686"
    assert not any(d["metadata"]["name"] == "phoenix" for d in docs)
    rc, _, docs = template()
    assert rc == 0 and "RAMEN_OTEL_UI_URL" not in console_env(docs)
    rc, err, _ = template("otel.collector=tempo")
    assert rc != 0 and "none, jaeger or phoenix" in err


@needs_helm
def test_phoenix_keeps_its_span_history_on_a_volume_across_restarts():
    """b35: a CA switch restarts Phoenix (as do upgrades); its SQLite store was an emptyDir, so every restart lost the
    whole span history. A PVC by default (RWO, hence maxSurge 0); `persistence.enabled=false` keeps the old emptyDir."""
    rc, _, docs = template("otel.collector=phoenix")
    assert rc == 0
    pvc = next(d for d in docs if d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"] == "phoenix")
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "5Gi"
    assert "storageClassName" not in pvc["spec"]  # "" = the cluster default
    dep = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "phoenix")
    # one RWO volume, one writer: the old pod goes first; no switch to Recreate (server-side apply refuses it, b36)
    assert dep["spec"]["strategy"] == {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1}}
    pod = dep["spec"]["template"]["spec"]
    vols = {v["name"]: v for v in pod["volumes"]}
    assert vols["data"] == {"name": "data", "persistentVolumeClaim": {"claimName": "phoenix"}}
    by = {c["name"]: c for c in pod["containers"]}
    env = {e["name"]: e["value"] for e in by["phoenix"]["env"]}
    assert {"name": "data", "mountPath": env["PHOENIX_WORKING_DIR"]} in by["phoenix"]["volumeMounts"]
    assert pod["securityContext"]["fsGroup"] == 65532  # the volume is writable by the nonroot image
    rc, _, docs = template(
        "otel.collector=phoenix", "otel.phoenix.persistence.size=20Gi", "otel.phoenix.persistence.storageClass=premium"
    )
    pvc = next(d for d in docs if d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"] == "phoenix")
    assert pvc["spec"]["storageClassName"] == "premium" and pvc["spec"]["resources"]["requests"]["storage"] == "20Gi"

    def has_pvc(docs):
        return any(d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"] == "phoenix" for d in docs)

    rc, _, docs = template("otel.collector=phoenix", "otel.phoenix.persistence.enabled=false")
    assert rc == 0 and not has_pvc(docs)
    dep = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "phoenix")
    vols = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
    assert vols["data"] == {"name": "data", "emptyDir": {}}
    rc, _, docs = template("otel.collector=jaeger")
    assert not has_pvc(docs)


def test_the_config_page_names_the_trace_ui_whatever_the_chart_runs(demo, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RAMEN_OTEL_UI_URL", "http://phoenix.ramen-system:6006")
    page = demo.get("/config").text
    assert 'href="http://phoenix.ramen-system:6006"' in page and "Trace UI" in page and "Jaeger UI" not in page
