"""Secrets backend (CONTRACTS §7): create → Secret Manager secret `ramen-<group>-<env>-<zone>-<NAME>` exists with
labels, value never returned by the console, delete → gone. Needs RAMEN_CONSOLE_URL; SM checks need
RAMEN_GCP_PROJECT and a console running RAMEN_SECRETS_BACKEND=gcp."""

import pytest

from ramen_tests import gcp
from ramen_tests.console import items

from .conftest import ENV, GROUP, ZONE, ok, poll

pytestmark = pytest.mark.cloud
VALUE = "cloud-secret-value-never-shown-4f2a"


@pytest.fixture(scope="module")
def secret(admin, world, suffix):
    name = f"CLOUDTEST_{suffix.upper()}"
    r = ok(admin.add_secret(GROUP, name, VALUE, env=ENV, zone=ZONE), 201)
    doc = r.json()
    yield {"name": name, "id": doc["id"], "create_response": r.text}
    admin.delete("secret", group=GROUP, id=doc["id"])


@pytest.fixture(scope="module")
def sm_enabled(admin, gcp_project):
    cfg = ok(admin.get("config")).json()
    backend = cfg.get("env", {}).get("RAMEN_SECRETS_BACKEND", "store")
    if backend != "gcp":
        pytest.skip(f"console RAMEN_SECRETS_BACKEND={backend}; Secret Manager assertions need gcp")
    return True


def test_value_never_returned(admin, secret):
    assert VALUE not in secret["create_response"]
    listing = ok(admin.get("secrets", group=GROUP))
    assert VALUE not in listing.text
    mine = next(s for s in items(listing) if s["name"] == secret["name"])
    assert "value" not in mine and mine.get("env") == ENV and mine.get("zone") == ZONE
    for r in (admin.get("secrets", group=GROUP, params={"format": "csv"}), admin.audit(), admin.page("/secrets")):
        assert VALUE not in r.text


def test_secret_scoped_listing(admin, secret):
    names = [s["name"] for s in items(ok(admin.get("secrets", group=GROUP, params={"env": ENV, "zone": ZONE})))]
    assert secret["name"] in names
    other = [s["name"] for s in items(ok(admin.get("secrets", group=GROUP, params={"env": "nope-env"})))]
    assert secret["name"] not in other


def test_secret_manager_secret_exists(secret, gcp_project, sm_enabled):
    sm = gcp.secret_name(GROUP, ENV, ZONE, secret["name"])
    desc = poll(lambda: gcp.get("secrets", "describe", sm, project=gcp_project), timeout=60, what=f"SM secret {sm}")
    labels = desc.get("labels", {})
    assert labels.get("group") == GROUP and labels.get("env") == ENV and labels.get("zone") == ZONE, labels
    versions = gcp.get("secrets", "versions", "list", sm, project=gcp_project) or []
    assert any(v.get("state") == "ENABLED" for v in versions), versions


def test_delete_removes_secret_everywhere(admin, world, suffix, gcp_project, sm_enabled):
    name = f"CLOUDDEL_{suffix.upper()}"
    sid = ok(admin.add_secret(GROUP, name, VALUE, env=ENV, zone=ZONE), 201).json()["id"]
    sm = gcp.secret_name(GROUP, ENV, ZONE, name)
    poll(lambda: gcp.get("secrets", "describe", sm, project=gcp_project), timeout=60, what=f"SM secret {sm}")
    ok(admin.delete("secret", group=GROUP, id=sid))
    assert name not in [s["name"] for s in items(ok(admin.get("secrets", group=GROUP)))]
    poll(lambda: gcp.get("secrets", "describe", sm, project=gcp_project) is None, timeout=60, what=f"{sm} deleted")


def test_delete_removes_secret_from_store(admin, world, suffix):
    name = f"LOCALDEL_{suffix.upper()}"
    sid = ok(admin.add_secret(GROUP, name, VALUE), 201).json()["id"]
    ok(admin.delete("secret", group=GROUP, id=sid))
    assert name not in [s["name"] for s in items(ok(admin.get("secrets", group=GROUP)))]
    assert admin.delete("secret", group=GROUP, id=sid).status_code == 404
