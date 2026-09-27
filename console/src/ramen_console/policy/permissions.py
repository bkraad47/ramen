"""SA policy engine (CONTRACTS §9, F4.2/F6.5): abstract permissions → cloud roles/actions, and rule evaluation.
Rules: `[{effect: allow|deny, permission: <glob>}]`. Deny wins; when a rule set has allow rules the permission
must match one of them. Super-admin rules gate what group admins may request; group rules add their own limits."""
from fnmatch import fnmatch

PERMISSIONS: dict[str, dict] = {
    "bucket.read": {"desc": "read the group's code bucket", "gcp": ["roles/storage.objectViewer"],
                    "aws": ["s3:GetObject", "s3:ListBucket"]},
    "bucket.write": {"desc": "write objects in the group's bucket", "gcp": ["roles/storage.objectUser"],
                     "aws": ["s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]},
    "secrets.read": {"desc": "read the group's secrets", "gcp": ["roles/secretmanager.secretAccessor"],
                     "aws": ["secretsmanager:GetSecretValue"]},
    "logs.write": {"desc": "write application logs", "gcp": ["roles/logging.logWriter"],
                   "aws": ["logs:CreateLogStream", "logs:PutLogEvents"]},
    "metrics.write": {"desc": "write custom metrics", "gcp": ["roles/monitoring.metricWriter"],
                      "aws": ["cloudwatch:PutMetricData"]},
    "pubsub.publish": {"desc": "publish messages", "gcp": ["roles/pubsub.publisher"], "aws": ["sns:Publish"]},
    "queue.consume": {"desc": "consume queue messages", "gcp": ["roles/pubsub.subscriber"],
                      "aws": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]},
    "datastore.user": {"desc": "read/write the cloud document DB", "gcp": ["roles/datastore.user"],
                       "aws": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]},
    "kms.decrypt": {"desc": "decrypt with the group's keys", "gcp": ["roles/cloudkms.cryptoKeyDecrypter"],
                    "aws": ["kms:Decrypt"]},
    "ai.user": {"desc": "call managed AI models", "gcp": ["roles/aiplatform.user"], "aws": ["bedrock:InvokeModel"]},
}


def known(permission: str) -> bool:
    return permission in PERMISSIONS


def mapped(permissions: list[str], provider: str) -> list[str]:
    out: list[str] = []
    for p in permissions:
        for r in PERMISSIONS.get(p, {}).get(provider, []):
            if r not in out:
                out.append(r)
    return out


def table() -> list[dict]:
    return [{"permission": k, **v} for k, v in PERMISSIONS.items()]


def _denied_by(permission: str, rules: list[dict], who: str) -> str | None:
    for r in rules:
        if r.get("effect") == "deny" and fnmatch(permission, r.get("permission", "")):
            raise_ = f"{who} rule denies {r['permission']!r}"
            return raise_
    allows = [r["permission"] for r in rules if r.get("effect") == "allow"]
    if allows and not any(fnmatch(permission, a) for a in allows):
        return f"{who} rules allow only {', '.join(repr(a) for a in allows)}"
    return None


def evaluate(permission: str, super_rules: list[dict], group_rules: list[dict]) -> str | None:
    """None when the request may proceed, else the reason it is denied."""
    if not known(permission):
        return f"unknown permission {permission!r}"
    return _denied_by(permission, super_rules or [], "super-admin") or _denied_by(permission, group_rules or [], "group")
