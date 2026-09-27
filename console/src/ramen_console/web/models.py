from pydantic import BaseModel, field_validator


def _split(v):
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
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


class GroupUpdate(BaseModel):
    repo_url: str | None = None
    ref: str | None = None
    mcp_auth: dict | None = None
    github_token: str | None = None


class Rules(BaseModel):
    rules: list[dict]


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


class UserUpdate(ListFields):
    role: str | None = None
    groups: list[str] | None = None


class Password(BaseModel):
    password: str


class KeyIn(ListFields):
    name: str
    role: str | None = None
    groups: list[str] | None = None


class RequestIn(BaseModel):
    role: str
    group: str | None = None


class BackupIn(BaseModel):
    target: str = "local"
    path: str | None = None
