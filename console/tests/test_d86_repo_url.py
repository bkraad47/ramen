"""§39.5a N1 (b33 residual): a group's `repo_url` is masked where it is shown when its userinfo carries a credential;
an ssh user (`ssh://git@host/…`, scp-like `git@host:…`) is not one and stays readable."""

import pytest

from ramen_console import redact, security
from tests.test_api import app, client, cloud, root  # noqa: F401 - fixtures

TOKEN = "ghp_tokentokentoken42"
SECRET_URL = f"https://{TOKEN}@github.com/o/r.git"


@pytest.mark.parametrize(
    "raw, shown",
    [
        (SECRET_URL, "https://***@github.com/o/r.git"),
        ("https://user:pa55@gitlab.com/o/r.git", "https://***@gitlab.com/o/r.git"),
        ("ssh://git@github.com/o/r.git", "ssh://git@github.com/o/r.git"),
        ("SSH://git@github.com/o/r.git", "SSH://git@github.com/o/r.git"),
        ("git+ssh://deploy@host/r", "git+ssh://deploy@host/r"),
        ("ssh://git:pw@host/r", "ssh://***@host/r"),
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("https://github.com/o/r.git", "https://github.com/o/r.git"),
        ("", ""),
    ],
)
def test_repo_url_masks_only_credentials(raw, shown):
    assert redact.redact(raw) == shown


def test_an_ssh_user_is_not_remembered_as_a_secret():
    assert redact.url_secrets("ssh://git@github.com/o/r") == []
    assert redact.url_secrets(SECRET_URL) == [TOKEN]


def test_the_deploy_log_masks_the_token_and_keeps_an_ssh_url():
    log = security.ScrubbedLog()
    log.append(f"t0 syncing repo {SECRET_URL}")
    log.append("t1 syncing repo ssh://git@github.com/o/r.git")
    assert TOKEN not in log[0] and "https://***@github.com/o/r.git" in log[0]
    assert log[1].endswith("ssh://git@github.com/o/r.git")


def test_group_api_and_page_mask_the_repo_token_and_a_masked_post_back_keeps_it(root):  # noqa: F811
    r = root.post("/api/v1/groups", json={"name": "priv", "repo_url": SECRET_URL})
    assert r.status_code == 201 and r.json()["repo_url"] == "https://***@github.com/o/r.git"
    for path in ("/api/v1/groups", "/api/v1/groups/priv", "/groups", "/groups/priv"):
        body = root.get(path).text
        assert TOKEN not in body and "https://***@github.com/o/r.git" in body, path
    r = root.put("/api/v1/groups/priv", json={"repo_url": "https://***@github.com/o/r.git", "ref": "dev"})
    assert r.status_code == 200, r.text
    stored = root.portal.call(root.app.state.store.get, "groups", "priv")
    assert stored["repo_url"] == SECRET_URL and stored["ref"] == "dev"  # the masked value meant "unchanged"
    r = root.put("/api/v1/groups/priv", json={"repo_url": "ssh://git@github.com/o/r.git"})
    assert r.json()["repo_url"] == "ssh://git@github.com/o/r.git"
