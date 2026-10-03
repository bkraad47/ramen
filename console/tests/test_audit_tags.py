"""N4: every action log line is properly tagged (CONTRACTS: `note()` tags are the filterable dimension;
`target` alone is not enough — several mutating routes were logging with an empty `tags` list)."""

from tests.test_api import app, client, cloud, demo, make_user, root  # noqa: F401 - fixtures


def last(client, action: str) -> dict:
    rows = [a for a in client.get("/api/v1/audit").json() if a["action"] == action]
    assert rows, f"no audit row for {action}"
    return sorted(rows, key=lambda a: a["ts"])[-1]


def test_every_mutating_route_logs_a_non_empty_tag(demo):
    demo.post("/api/v1/zones", json={"name": "zone-c"})
    assert last(demo, "zone.create")["tags"] == ["zone:zone-c"]
    demo.delete("/api/v1/zones/zone-c")
    assert last(demo, "zone.delete")["tags"] == ["zone:zone-c"]

    u = make_user(demo, "t@x", "viewer", ["demo"])
    demo.put(f"/api/v1/users/{u['id']}", json={"role": "group_admin", "groups": ["demo"]})
    assert last(demo, "user.update")["tags"] == ["role:group_admin", "group:demo"]
    demo.post(f"/api/v1/users/{u['id']}/password", json={"password": "N3wPassw0rd!-here"})
    assert last(demo, "user.password")["tags"] == [f"user:{u['id']}"]
    demo.delete(f"/api/v1/users/{u['id']}")
    assert last(demo, "user.delete")["tags"] == [f"user:{u['id']}"]

    kid = demo.post("/api/v1/api-keys", json={"name": "ci"}).json()["id"]
    demo.delete(f"/api/v1/api-keys/{kid}")
    assert last(demo, "api_key.delete")["tags"] == [f"key:{kid}"]

    rid = demo.post("/api/v1/requests", json={"group": "demo", "zone": "zone-a", "permission": "logs.write"}).json()[
        "id"
    ]
    demo.post(f"/api/v1/requests/{rid}/approve")
    assert last(demo, "permission.approve")["tags"] == [f"request:{rid}"]
    demo.post(f"/api/v1/requests/{rid}/revoke")
    assert last(demo, "permission.revoke")["tags"] == [f"request:{rid}"]

    rid2 = demo.post("/api/v1/requests", json={"group": "demo", "zone": "zone-a", "permission": "logs.write"}).json()[
        "id"
    ]
    demo.post(f"/api/v1/requests/{rid2}/deny")
    assert last(demo, "permission.deny")["tags"] == [f"request:{rid2}"]

    cid = demo.post(
        "/api/v1/oauth/clients", json={"name": "cli", "redirect_uris": ["http://127.0.0.1:9999/cb"]}
    ).json()["client_id"]
    assert last(demo, "oauth.client.create")["tags"] == ["client:cli"]
    demo.delete(f"/api/v1/oauth/clients/{cid}")
    assert last(demo, "oauth.client.delete")["tags"] == [f"client:{cid}"]

    demo.post("/api/v1/backups", json={"target": "local"})
    assert last(demo, "backup.create")["tags"] == ["target:local"]

    demo.put("/api/v1/config/sa-rules", json={"rules": [{"effect": "deny", "permission": "kms.*"}]})
    assert last(demo, "config.sa_rules")["tags"] == ["rules:1"]

    demo.post("/api/v1/refresh")
    assert last(demo, "refresh")["tags"] == ["scope:cloud"]

    demo.post("/api/v1/config/reload")
    assert last(demo, "config.reload")["tags"] == ["scope:global"]
