"""CONTRACTS §13.1 against a populated cloud deployment (V5.1): a backup to the groups bucket, a restore that
prunes and reconciles, and live zones that keep serving afterwards. Needs RAMEN_CONSOLE_URL (+ RAMEN_NODE_URL)."""

import pytest

from ramen_tests import env as E
from ramen_tests.console import items

from .conftest import GROUP, ZONE, ok, poll

pytestmark = pytest.mark.cloud
S_OK = "ok"


@pytest.fixture(scope="module")
def backup(admin, world, suffix) -> dict:
    """Two workers in the zone, then a backup in the bucket — the state the restore has to bring back."""
    ok(admin.put("workers", {"count": 2}, group=GROUP, zone=ZONE))
    b = ok(admin.post("backups", {"target": "bucket"}), 201).json()
    assert b["target"] == "bucket" and b["path"].startswith(("gs://", "/", "s3://", "./")), b
    return b


def test_the_backup_carries_the_state_and_no_secrets(admin, backup):
    body = ok(admin.get("backup_download", id=backup["id"])).text
    assert GROUP in body and ZONE in body
    for leak in ("password_hash", "rmk_", "rmn_", '"value"'):
        assert leak not in body, leak


def test_restore_prunes_what_came_later_reconciles_the_zone_and_leaves_it_serving(
    admin, backup, node_opt, gcp_project, suffix
):
    probe = f"probe-{suffix}"[:40]
    ok(admin.create_group(probe, "https://example.com/none.git"), 201)
    ok(admin.put("workers", {"count": 1}, group=GROUP, zone=ZONE))

    plan = ok(admin.post("backup_restore", {"dry_run": True}, id=backup["id"])).json()
    assert probe in plan["extra"]["groups"] and plan["dry_run"] is True
    assert ok(admin.get("group", group=probe)).status_code == 200  # a preview writes nothing

    r = ok(admin.post("backup_restore", {"prune": True, "reconcile": True}, id=backup["id"])).json()
    assert r["pruned"]["groups"] == [probe]
    assert f"{GROUP}/{ZONE}" in r["reconciled"], r["reconciled"]
    assert admin.get("group", group=probe).status_code == 404
    assert ok(admin.get("workers", group=GROUP, zone=ZONE)).json()["count"] == 2

    if node_opt:  # the reconcile re-applied the zone: the worker is still there and still answers
        poll(lambda: node_opt.health() == "SERVING", timeout=300, what="zone SERVING after restore")
        assert node_opt.status({"jsonrpc": "2.0", "id": 1, "method": "ping"}).name == "OK"
    live = poll(
        lambda: [w for w in ok(admin.get("workers", group=GROUP, zone=ZONE)).json().get("live", []) if w],
        timeout=300,
        what="live workers after restore",
    )
    assert live


def test_restore_is_audited_with_its_flags(admin, backup):
    rows = [e for e in items(ok(admin.audit())) if e.get("action") == "backup.restore"]
    assert rows and any("prune:true" in (e.get("tags") or []) for e in rows)


@pytest.mark.skipif(not E.env("RAMEN_STORE_KIND"), reason="set RAMEN_STORE_KIND=firestore|dynamodb to assert the store")
def test_the_restore_ran_against_the_cloud_store(admin):
    assert ok(admin.get("config")).json()["store"] == E.env("RAMEN_STORE_KIND")
