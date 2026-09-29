import pytest

from ramen_console.errors import ApiError
from ramen_console.secrets import make_secrets_backend
from ramen_console.secrets.aws import AwsSecrets, secret_name
from tests.fakes_aws import FakeAwsClients, aws_env


@pytest.fixture
def fk():
    with aws_env():
        yield FakeAwsClients()


def test_secret_name():
    assert secret_name("demo", "prod", None, "TOKEN") == "ramen/demo/prod/all/TOKEN"
    assert secret_name("demo", None, "a", "ci", kind="mcp_key") == "ramen/demo/all/a/mcp-ci"


async def test_aws_backend_roundtrip(fk):
    sm = fk.secretsmanager
    b = AwsSecrets("us-east-1", sm)
    doc = await b.put("demo", "prod", None, "TOKEN", "s3cret")
    assert doc == {"value": None, "ref": "asm://ramen/demo/prod/all/TOKEN"}
    d = sm.describe_secret(SecretId="ramen/demo/prod/all/TOKEN")
    assert {t["Key"]: t["Value"] for t in d["Tags"]} == {
        "ramen": "secret",
        "group": "demo",
        "env": "prod",
        "zone": "all",
    }
    doc2 = await b.put("demo", "prod", None, "TOKEN", "v2")  # exists → new version
    assert doc2 == doc and await b.resolve(doc["ref"]) == "v2"
    assert (
        await b.resolve("plain") == "plain"
        and await b.resolve(None) is None
        and await b.resolve("sm://projects/p/secrets/x") == "sm://projects/p/secrets/x"
    )
    assert await b.resolve_config({"RAMEN_MCP_KEYS": f"{doc['ref']},{doc['ref']}", "X": "1"}) == {
        "RAMEN_MCP_KEYS": "v2,v2",
        "X": "1",
    }
    await b.delete(doc)
    assert not sm.list_secrets()["SecretList"]
    await b.delete(doc)  # gone: ignored
    await b.delete({"ref": None})
    await b.delete({"ref": "sm://projects/p/secrets/x"})  # not ours: ignored
    with pytest.raises(ApiError) as e:
        await b.resolve(doc["ref"])
    assert e.value.status_code == 502 and "v2" not in e.value.detail
    # a secret scheduled for deletion is restored and overwritten
    await b.put("demo", "prod", None, "T2", "one")
    sm.delete_secret(SecretId="ramen/demo/prod/all/T2")
    await b.put("demo", "prod", None, "T2", "two")
    assert await b.resolve("asm://ramen/demo/prod/all/T2") == "two"


async def test_aws_backend_errors(fk):
    from botocore.exceptions import ClientError

    class Boom:
        def create_secret(self, **kw):
            raise ClientError({"Error": {"Code": "InternalServiceError", "Message": "x"}}, "CreateSecret")

        def delete_secret(self, **kw):
            raise ClientError({"Error": {"Code": "InternalServiceError", "Message": "x"}}, "DeleteSecret")

    b = AwsSecrets("us-east-1", Boom())
    with pytest.raises(ApiError, match="Secrets Manager"):
        await b.put("g", None, None, "A", "v")
    with pytest.raises(ApiError, match="Secrets Manager"):
        await b.delete({"ref": "asm://ramen/g/all/all/A"})


def test_factory(monkeypatch, fk):
    monkeypatch.setenv("RAMEN_SECRETS_BACKEND", "aws")
    monkeypatch.setenv("RAMEN_AWS_REGION", "eu-west-1")
    b = make_secrets_backend(cloud=type("C", (), {"c": fk})())
    assert isinstance(b, AwsSecrets) and b.client is fk.secretsmanager and b.region == "eu-west-1"
    monkeypatch.setattr("ramen_console.cloud.aws_api.AwsClients.secretsmanager", property(lambda self: "real"))
    assert make_secrets_backend().client == "real"
