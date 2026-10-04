import json
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator


def _split(v):
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    if isinstance(v, list):  # a checkbox group posts a hidden "" so an empty choice still reaches the API
        return [s for s in v if s != ""]
    return v


class ListFields(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def _lists(cls, v, info):
        ann = cls.model_fields[info.field_name].annotation
        return _split(v) if "list" in str(ann) else v


class GroupIn(BaseModel):
    name: str
    repo_url: str = ""
    ref: str = "main"
    github_token: str | None = None
    github_app_installation_id: str | None = None


class GroupUpdate(BaseModel):
    repo_url: str | None = None
    ref: str | None = None
    mcp_auth: dict | None = None
    github_token: str | None = None
    github_app_installation_id: str | None = None


class Rules(BaseModel):
    """`rules` as a list, or as the JSON string a form's textarea/hidden field posts; `effect` + `glob`/`permission`
    append one rule to it (the Config page's "Add rule" — no `eval` needed in the browser)."""

    rules: list[dict]

    @model_validator(mode="before")
    @classmethod
    def _from_form(cls, data):
        if not isinstance(data, dict):
            return data
        rules = data.get("rules", [])
        if isinstance(rules, str):
            try:
                rules = json.loads(rules or "[]")
            except ValueError as e:
                raise ValueError(f"rules is not valid JSON: {e}") from e
        if data.get("effect"):
            permission = (data.get("glob") or "").strip() or data.get("permission") or "*"
            rules = list(rules) + [{"effect": data["effect"], "permission": permission}]
        return {"rules": rules}


class ZoneIn(BaseModel):
    name: str
    provider: str = "local"
    region: str = ""


class EnvIn(ListFields):
    name: str
    ref: str | None = None
    zones: list[str] = []


class EnvUpdate(ListFields):
    ref: str | None = None
    zones: list[str] | None = None
    verbose: bool | None = None
    blocked: list[str] | None = None


class Blocked(ListFields):
    blocked: list[str]


class Verbose(BaseModel):
    verbose: bool


class DeployIn(BaseModel):
    zone: str | None = None
    canary: bool = True


class WorkersIn(ListFields):
    count: int | None = None
    size: str | None = None
    allowed_sizes: list[str] | None = None


class Cidrs(ListFields):
    cidrs: list[str]


class ThrottleIn(BaseModel):
    redis_url: str | None = None
    ip_per_min: int | None = None
    token_per_min: int | None = None


class SecretIn(BaseModel):
    name: str
    value: str
    env: str | None = None
    zone: str | None = None


class Named(BaseModel):
    name: str


class UserIn(ListFields):
    email: str
    password: str
    role: str = "viewer"
    groups: list[str] = []
    memberships: dict[str, str] | None = None  # D41: {group: role}; wins over role + groups when given


class UserUpdate(ListFields):
    role: str | None = None
    groups: list[str] | None = None
    memberships: dict[str, str] | None = None


class MemberIn(BaseModel):
    role: str


class MemberAdd(BaseModel):
    email: str
    role: str


class Password(BaseModel):
    password: str


class KeyIn(ListFields):
    name: str
    role: str | None = None
    groups: list[str] | None = None
    client_type: str = "devops"


class RequestIn(BaseModel):
    role: str | None = None
    group: str | None = None
    zone: str | None = None
    permission: str | None = None
    scope: str | None = None  # resource names for a permission request; empty or `*` = the group's own area


class AuthConfig(BaseModel):
    password_login: bool | None = None
    magic_link: bool | None = None


class RoleMapChange(ListFields):
    """One edit to a provider's role mapping (D38): name the claim, add/replace a rule, or remove one."""

    claim: str | None = None
    value: str | None = None
    role: Literal["super_admin", "group_admin", "viewer", "mcp_user"] | None = None
    groups: list[str] | None = None
    remove: str | None = None


class BaseUriConfig(BaseModel):
    base_uri: str = ""


class SchedulerConfig(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = None


class SmtpConfig(BaseModel):
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    mail_from: str | None = None
    tls: str | None = None


class GitHubAppConfig(BaseModel):
    app_id: str | None = None
    private_key: str | None = None


class NotifyConfig(ListFields):
    user_ids: list[str]


class ImageIn(BaseModel):
    tag: str
    digest: str | None = None
    note: str = ""


class ImageRecall(BaseModel):
    id: str


class OAuthClientIn(ListFields):
    name: str
    redirect_uris: list[str]


class BackupIn(BaseModel):
    target: str = "local"
    path: str | None = None


class RestoreIn(BaseModel):
    dry_run: bool = False
    prune: bool = False
    reconcile: bool = False
    force: bool = False
