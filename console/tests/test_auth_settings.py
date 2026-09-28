import pytest

from ramen_console.auth.oauth import OAuthRegistry, metadata_url
from ramen_console.auth.settings import AuthSettings, parse_role, parse_role_map
from ramen_console.auth.tokens import Tokens
from ramen_console.policy import permissions as perm


def test_settings_from_env_and_doc():
    s = AuthSettings.from_env(
        {
            "RAMEN_ADMIN_EMAIL": "Root@X ",
            "RAMEN_AUTH_PASSWORD_LOGIN": "false",
            "RAMEN_AUTH_MAGIC_LINK": "yes",
            "RAMEN_ADMIN_FORCE_PASSWORD": "1",
            "RAMEN_AUTH_OAUTH_OIDC_ROLE_CLAIM": "groups",
            "RAMEN_AUTH_OAUTH_OIDC_ROLE_MAP": "Admins=super_admin; devs=group_admin:demo,other",
            "RAMEN_AUTH_OAUTH_OIDC_ROLE_MAP_OPS": "viewer:demo",
            "OTHER": "x",
        }
    )
    assert (s.password_login, s.magic_link, s.force_password, s.admin_email) == (False, True, True, "root@x")
    assert s.role_claim == {"oidc": "groups"}
    assert s.role_map["oidc"]["admins"] == ("super_admin", []) and s.role_map["oidc"]["ops"] == ("viewer", ["demo"])
    assert s.can_password("root@x") and not s.can_password("bob@x") and not s.can_password("")
    assert s.map_role("oidc", {"groups": ["devs", "ops"]}) == ("group_admin", ["demo", "other"])
    assert s.map_role("oidc", {"groups": "admins"}) == ("super_admin", [])
    assert s.map_role("oidc", {"groups": ["nobody"]}) == ("viewer", []) and s.map_role("nope", {}) == ("viewer", [])
    d = s.with_doc({"password_login": True})
    assert d.password_login and d.magic_link and d.force_password and d.can_password("bob@x")
    assert s.with_doc(None).password_login is False
    pub = s.public()
    assert pub["break_glass"] and pub["role_map"]["oidc"]["devs"] == {
        "role": "group_admin",
        "groups": ["demo", "other"],
    }
    assert AuthSettings.from_env({}).password_login and not AuthSettings.from_env({}).magic_link


def test_role_parsing_errors():
    assert parse_role("group_admin: a , b") == ("group_admin", ["a", "b"]) and parse_role("") == ("viewer", [])
    with pytest.raises(ValueError):
        parse_role("king")
    assert parse_role_map("") == {}


def test_tokens_roundtrip():
    t = Tokens("s")
    tok = t.issue("reset", "u1", "n1")
    assert t.load("reset", tok) == ("u1", "n1")
    assert t.load("magic", tok) is None and t.load("reset", tok + "x") is None and t.load("bogus", tok) is None
    assert Tokens("other").load("reset", tok) is None


def test_oauth_registry_issuer_and_reserved():
    assert metadata_url({"issuer": "https://idp/"}) == "https://idp/.well-known/openid-configuration"
    assert metadata_url({"metadata_url": "https://m"}) == "https://m" and metadata_url({}) is None
    reg = OAuthRegistry.from_env(
        {
            "RAMEN_OAUTH_IDP_ISSUER": "https://idp",
            "RAMEN_OAUTH_IDP_CLIENT_ID": "c",
            "RAMEN_OAUTH_IDP_CLIENT_SECRET": "s",
            "RAMEN_OAUTH_RESET_ISSUER": "https://x",
            "RAMEN_OAUTH_RESET_CLIENT_ID": "c",
            "RAMEN_OAUTH_RESET_CLIENT_SECRET": "s",
            "RAMEN_OAUTH_NOMETA_CLIENT_ID": "c",
            "RAMEN_OAUTH_NOMETA_CLIENT_SECRET": "s",
        }
    )
    assert reg.providers() == ["idp"] and reg.client("reset") is None


def test_permission_table_and_rules():
    assert perm.known("bucket.read") and not perm.known("root.everything")
    assert perm.mapped(["bucket.read", "secrets.read", "bucket.read", "nope"], "gcp") == [
        "roles/storage.objectViewer",
        "roles/secretmanager.secretAccessor",
    ]
    assert "s3:GetObject" in perm.mapped(["bucket.read"], "aws")
    assert {"permission", "desc", "gcp", "aws"} <= set(perm.table()[0])
    assert perm.evaluate("bucket.read", [], []) is None
    assert "unknown" in perm.evaluate("nope", [], [])
    assert "super-admin rule denies" in perm.evaluate("kms.decrypt", [{"effect": "deny", "permission": "kms.*"}], [])
    assert "allow only" in perm.evaluate("kms.decrypt", [{"effect": "allow", "permission": "bucket.*"}], [])
    assert perm.evaluate("bucket.read", [{"effect": "allow", "permission": "bucket.*"}], []) is None
    assert "group rule denies" in perm.evaluate("bucket.write", [], [{"effect": "deny", "permission": "bucket.write"}])
