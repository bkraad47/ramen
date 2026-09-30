"""v0.5.5 I2: the group-create form/flow must take a git access token up front (private GitHub/GitLab repos),
and the groups page's Create action must be a collapsed form box, not always-open."""

import time

from tests.test_api import PW, app, client, cloud, root  # noqa: F401 - pytest fixtures


def wait_job(client, jid):
    for _ in range(100):
        j = client.get(f"/api/v1/jobs/{jid}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job still running")


def test_create_group_accepts_and_stores_a_token(root):
    r = root.post(
        "/api/v1/groups",
        json={
            "name": "priv",
            "repo_url": "https://gitlab.com/org/priv.git",
            "ref": "main",
            "github_token": "glpat-xyz",
        },
    )
    assert r.status_code == 201, r.text
    assert "github_token" not in r.json()  # never echoed back (util.public hides it)
    g = root.get("/api/v1/groups/priv").json()
    assert "github_token" not in g


def test_the_token_set_at_create_is_what_deploy_resolves(root):
    """A GitLab repo token supplied on the create form must reach cloud.sync_repo unchanged — before this fix
    create_group() silently dropped it and only a follow-up PUT could ever set it."""
    seen = {}

    async def fake_sync(group, repo_url, ref, token):
        seen["token"] = token
        return "/tmp/fake"

    root.app.state.cloud.sync_repo = fake_sync
    root.post(
        "/api/v1/groups",
        json={
            "name": "priv2",
            "repo_url": "https://gitlab.com/org/priv2.git",
            "ref": "main",
            "github_token": "glpat-abc",
        },
    )
    root.post("/api/v1/groups/priv2/environments", json={"name": "prod", "zones": []})
    # sync_repo runs before the (irrelevant here) per-zone worker loop, so an empty zone list still exercises it
    wait_job(root, root.post("/api/v1/groups/priv2/environments/prod/deploy", json={"canary": False}).json()["id"])
    assert seen["token"] == "glpat-abc"


def test_groups_page_create_form_is_collapsed_behind_a_button(root):
    page = root.get("/groups").text
    assert "<details" in page and "Create group" in page
    assert 'name="github_token"' in page
    # the form fields (repo, ref, token) live inside the collapsed box, not floating in the open page body
    details_start = page.index("<details")
    details_end = page.index("</details>")
    box = page[details_start:details_end]
    assert 'name="repo_url"' in box and 'name="ref"' in box and 'name="github_token"' in box
