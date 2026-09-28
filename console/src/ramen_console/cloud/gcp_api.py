"""GCS repo sync, Cloud Logging, Cloud Armor / backend capacity and IAM helpers (sync functions, run in threads)."""

import base64
import hashlib
import json
import re
import tempfile
import time
from pathlib import Path

from ..errors import ApiError
from .gcp_clients import fresh_http, http_status
from .local import redact, run_git

DEFAULT_PRIORITY = 2147483647


def sync_repo_to_gcs(storage, bucket_name, group, repo_url, ref, token) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        r = run_git(["git", "clone", "-q", "--depth", "1", "--branch", ref, repo_url, tmp], token)
        if r.returncode != 0:
            raise ApiError(502, "git failed: " + redact(r.stderr, token).strip()[:500])
        root, prefix = Path(tmp), f"{group}/"
        local = {}
        for p in root.rglob("*"):
            if p.is_file() and ".git" not in p.relative_to(root).parts:
                local[prefix + p.relative_to(root).as_posix()] = p
        bucket = storage.bucket(bucket_name)
        remote = {b.name: b for b in bucket.list_blobs(prefix=prefix)}
        for name, path in local.items():
            digest = base64.b64encode(hashlib.md5(path.read_bytes()).digest()).decode()
            if name not in remote or remote[name].md5_hash != digest:
                bucket.blob(name).upload_from_filename(str(path))
        for name, blob in remote.items():
            if name not in local:
                blob.delete()
    return f"gs://{bucket_name}/{group}"


_K8S_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")


def k8s_name(value: str, what: str) -> str:
    """Namespace/pod names only: anything else could rewrite a log filter or query (SEC-02)."""
    if not isinstance(value, str) or not _K8S_NAME.match(value):
        raise ApiError(422, f"invalid {what} name")
    return value


def fetch_logs(client, ns, worker, tail) -> str:
    ns = k8s_name(ns, "namespace")
    worker = k8s_name(worker, "worker") if worker else None
    f = f'resource.type="k8s_container" AND resource.labels.namespace_name="{ns}"'
    if worker:
        f += f' AND resource.labels.pod_name="{worker}"'
    entries = list(
        client.list_entries(filter_=f, order_by="timestamp desc", max_results=tail, page_size=min(tail, 1000))
    )
    lines = []
    for e in reversed(entries):
        payload = e.payload if isinstance(e.payload, str) else json.dumps(e.payload)
        pod = (getattr(e.resource, "labels", None) or {}).get("pod_name", "-")
        ts = e.timestamp.isoformat(timespec="seconds") if e.timestamp else "-"
        lines.append(f"{ts} {e.severity or '-'} {pod} {payload}")
    return "\n".join(lines) + ("\n" if lines else "")


class Compute:
    def __init__(self, compute, project):
        self.api, self.project = compute, project

    def _wait(self, op):
        if isinstance(op, dict) and op.get("name") and op.get("status") != "DONE":
            self.api.globalOperations().wait(project=self.project, operation=op["name"]).execute(
                num_retries=3, http=fresh_http()
            )
        return op

    def _ready(self, make_request, attempts=24, delay=5.0):
        """Cloud Armor rejects rule edits with 400 'is not ready' for a while after the previous edit.

        Retry, then wait for the operation."""
        for i in range(attempts):
            try:
                return self._wait(make_request().execute(num_retries=3, http=fresh_http()))
            except Exception as e:  # noqa: BLE001
                if http_status(e) != 400 or "not ready" not in str(e) or i == attempts - 1:
                    raise
                time.sleep(delay)

    def backend_service(self, name) -> dict | None:
        try:
            return (
                self.api.backendServices()
                .get(project=self.project, backendService=name)
                .execute(num_retries=3, http=fresh_http())
            )
        except Exception as e:  # noqa: BLE001
            if http_status(e) == 404:
                return None
            raise

    def find_backend_service(self, neg_name, fallback) -> dict:
        """GKE Gateway auto-names backend services.

        Find the one whose backend group is NEG `neg_name`; else `fallback`."""
        token = None
        while True:
            page = (
                self.api.backendServices()
                .list(project=self.project, pageToken=token)
                .execute(num_retries=3, http=fresh_http())
            )
            for bs in page.get("items", []):
                if any(
                    b.get("group", "").endswith(f"/networkEndpointGroups/{neg_name}") for b in bs.get("backends", [])
                ):
                    return bs
            token = page.get("nextPageToken")
            if not token:
                break
        bs = self.backend_service(fallback)
        if bs is None:
            raise ApiError(
                404, f"no backend service found for NEG {neg_name} (Gateway route not programmed yet?) nor {fallback}"
            )
        return bs

    def set_capacity(self, bs: dict, neg_name, scaler, attempts=24) -> None:
        backends = bs.get("backends", [])
        for b in backends:
            if b.get("group", "").endswith(f"/networkEndpointGroups/{neg_name}"):
                b["capacityScaler"] = scaler
        body = {"backends": backends}
        if bs.get("fingerprint"):
            body["fingerprint"] = bs["fingerprint"]
        # Gateway-managed backend services report "not ready" while the controller reconciles them
        # (e.g. after a rollout)
        self._ready(
            lambda: self.api.backendServices().patch(project=self.project, backendService=bs["name"], body=body),
            attempts=attempts,
        )

    def set_armor(self, name, cidrs: list[str]) -> str:
        pols = self.api.securityPolicies()
        try:
            pol = pols.get(project=self.project, securityPolicy=name).execute(num_retries=3, http=fresh_http())
        except Exception as e:  # noqa: BLE001
            if http_status(e) != 404:
                raise
            self._ready(
                lambda: pols.insert(
                    project=self.project,
                    body={
                        "name": name,
                        "description": "ramen group allow-list",
                        "rules": [
                            {
                                "priority": DEFAULT_PRIORITY,
                                "action": "allow",
                                "description": "default",
                                "match": {"versionedExpr": "SRC_IPS_V1", "config": {"srcIpRanges": ["*"]}},
                            }
                        ],
                    },
                )
            )
            pol = pols.get(project=self.project, securityPolicy=name).execute(num_retries=3, http=fresh_http())
        for rule in pol.get("rules", []):
            if rule["priority"] != DEFAULT_PRIORITY:
                self._ready(
                    lambda r=rule: pols.removeRule(project=self.project, securityPolicy=name, priority=r["priority"])
                )
        for i in range(0, len(cidrs), 10):
            rule = {
                "priority": 1000 + i // 10,
                "action": "allow",
                "description": "ramen ip-rules",
                "match": {"versionedExpr": "SRC_IPS_V1", "config": {"srcIpRanges": cidrs[i : i + 10]}},
            }
            try:
                self._ready(lambda rule=rule: pols.addRule(project=self.project, securityPolicy=name, body=rule))
            except Exception as e:  # noqa: BLE001 - a retried add may already have landed: converge with a patch
                if http_status(e) != 400 or "same priorities" not in str(e):
                    raise
                self._ready(
                    lambda rule=rule: pols.patchRule(
                        project=self.project, securityPolicy=name, priority=rule["priority"], body=rule
                    )
                )
        self._ready(
            lambda: pols.patchRule(
                project=self.project,
                securityPolicy=name,
                priority=DEFAULT_PRIORITY,
                body={"action": "deny(403)" if cidrs else "allow"},
            )
        )
        return f"projects/{self.project}/global/securityPolicies/{name}"

    def attach_armor(self, backend_service, policy_ref, attempts=24) -> None:
        self._ready(
            lambda: self.api.backendServices().setSecurityPolicy(
                project=self.project, backendService=backend_service, body={"securityPolicy": policy_ref}
            ),
            attempts=attempts,
        )


class Iam:
    def __init__(self, iam, crm, project):
        self.iam, self.crm, self.project = iam, crm, project
        self._number = None

    def project_number(self) -> str:
        if self._number is None:
            self._number = str(
                self.crm.projects()
                .get(projectId=self.project)
                .execute(num_retries=3, http=fresh_http())["projectNumber"]
            )
        return self._number

    @staticmethod
    def account_id(group, zone) -> str:
        name = f"ramen-{group}-{zone}"
        return name if len(name) <= 30 else "ramen-" + hashlib.sha1(name.encode()).hexdigest()[:24]

    def ensure_account(self, account_id, display) -> tuple[str, bool]:
        email = f"{account_id}@{self.project}.iam.gserviceaccount.com"
        try:
            self.iam.projects().serviceAccounts().create(
                name=f"projects/{self.project}",
                body={"accountId": account_id, "serviceAccount": {"displayName": display}},
            ).execute(num_retries=3, http=fresh_http())
            return email, True
        except Exception as e:  # noqa: BLE001
            if http_status(e) != 409:
                raise
            return email, False

    def list_accounts(self) -> list[str]:
        r = (
            self.iam.projects()
            .serviceAccounts()
            .list(name=f"projects/{self.project}")
            .execute(num_retries=3, http=fresh_http())
        )
        return sorted(a["email"] for a in r.get("accounts", []) if a["email"].startswith("ramen-"))

    def list_service_accounts(self, prefix: str) -> list[str]:
        return [e for e in self.list_accounts() if e.startswith(prefix)]

    def delete_service_account(self, email: str) -> None:
        try:
            self.iam.projects().serviceAccounts().delete(
                name=f"projects/{self.project}/serviceAccounts/{email}"
            ).execute(num_retries=3, http=fresh_http())
        except Exception as e:  # noqa: BLE001
            if http_status(e) != 404:
                raise

    @staticmethod
    def _add(policy, role, member, condition=None) -> bool:
        for b in policy.setdefault("bindings", []):
            if b["role"] == role and (b.get("condition") or {}).get("expression") == (condition or {}).get(
                "expression"
            ):
                if member in b["members"]:
                    return False
                b["members"] = [*b["members"], member]  # google-cloud-storage policies use sets
                return True
        b = {"role": role, "members": [member]}
        if condition:
            b["condition"] = condition
        policy["bindings"].append(b)
        return True

    # resource-level bindings (SEC-08): the console holds no project-level setIamPolicy -------------------------
    def grant_bucket_roles(self, storage, bucket_name, email, bindings: list[tuple[str, dict | None]]) -> list[str]:
        """Bind roles on the groups bucket itself (conditions keep them to the group's prefix)."""
        bucket = storage.bucket(bucket_name)
        pol = bucket.get_iam_policy(requested_policy_version=3)
        doc = {"bindings": [dict(b) for b in pol.bindings]}
        if [role for role, cond in bindings if self._add(doc, role, f"serviceAccount:{email}", cond)]:
            pol.version = 3
            pol.bindings = doc["bindings"]
            # a freshly created GSA takes a few seconds to become visible to GCS IAM: 400 "… does not exist" until then
            for attempt in range(12):
                try:
                    bucket.set_iam_policy(pol)
                    break
                except Exception as e:  # noqa: BLE001
                    if http_status(e) != 400 or "does not exist" not in str(e) or attempt == 11:
                        raise
                    time.sleep(min(2**attempt, 10))
        return [r for r, _ in bindings]

    def group_secrets(self, sm, group) -> list[str]:
        prefix = f"ramen-{group}-"
        it = sm.list_secrets(request={"parent": f"projects/{self.project}", "filter": f"name:{prefix}"})
        return [s.name for s in it if s.name.rsplit("/", 1)[1].startswith(prefix)]

    @staticmethod
    def bind_secret(sm, name, roles: list[str], members: list[str]) -> bool:
        """Add `members` to `roles` on one secret; False when nothing changed (or the secret does not exist yet)."""
        try:
            pol = sm.get_iam_policy(request={"resource": name})
        except Exception as e:  # noqa: BLE001
            if http_status(e) == 404:
                return False
            raise
        get = lambda b, k: b[k] if isinstance(b, dict) else getattr(b, k)  # noqa: E731 - proto message or dict
        doc = [{"role": get(b, "role"), "members": list(get(b, "members"))} for b in pol.bindings]
        changed = False
        for role in roles:
            b = next((b for b in doc if b["role"] == role), None)
            if b is None:
                doc.append({"role": role, "members": list(members)})
                changed = True
                continue
            for m in members:
                if m not in b["members"]:
                    b["members"].append(m)
                    changed = True
        if changed:
            from google.iam.v1 import policy_pb2  # the policy is a proto message: bindings must be Binding messages

            del pol.bindings[:]
            pol.bindings.extend(policy_pb2.Binding(role=b["role"], members=b["members"]) for b in doc)
            sm.set_iam_policy(request={"resource": name, "policy": pol})
        return changed

    def grant_secret_roles(self, sm, group, email, roles: list[str]) -> list[str]:
        """Bind roles on every existing `ramen-<group>-*` secret (new secrets are bound at creation, secrets/gcp.py)."""
        for name in self.group_secrets(sm, group):
            self.bind_secret(sm, name, roles, [f"serviceAccount:{email}"])
        return list(roles)

    def group_members(self, group) -> list[str]:
        return [f"serviceAccount:{e}" for e in self.list_service_accounts(f"ramen-{group}-")]

    def grant_project_roles(self, email, bindings: list[tuple[str, dict]]) -> list[str]:
        """Project-wide roles (logging, monitoring, ...). Needs `roles/resourcemanager.projectIamAdmin`, which the
        Terraform module only grants with `console_project_iam = true`."""
        pol = (
            self.crm.projects()
            .getIamPolicy(resource=self.project, body={"options": {"requestedPolicyVersion": 3}})
            .execute(num_retries=3, http=fresh_http())
        )
        pol["version"] = 3
        changed = [role for role, cond in bindings if self._add(pol, role, f"serviceAccount:{email}", cond)]
        if changed:
            self.crm.projects().setIamPolicy(resource=self.project, body={"policy": pol}).execute(
                num_retries=3, http=fresh_http()
            )
        return [r for r, _ in bindings]

    def bind_workload_identity(self, email, ns, ksa) -> str:
        res = f"projects/{self.project}/serviceAccounts/{email}"
        member = f"serviceAccount:{self.project}.svc.id.goog[{ns}/{ksa}]"
        sa = self.iam.projects().serviceAccounts()
        pol = sa.getIamPolicy(resource=res).execute(num_retries=3, http=fresh_http())
        if self._add(pol, "roles/iam.workloadIdentityUser", member):
            sa.setIamPolicy(resource=res, body={"policy": pol}).execute(num_retries=3, http=fresh_http())
        return member
