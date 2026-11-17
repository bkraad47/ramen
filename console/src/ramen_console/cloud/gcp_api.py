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
from .local import redact, repo_files, run_git

DEFAULT_PRIORITY = 2147483647


def sync_repo_to_gcs(storage, bucket_name, group, repo_url, ref, token) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        r = run_git(["git", "clone", "-q", "--depth", "1", "--branch", ref, "--", repo_url, tmp], token)
        if r.returncode != 0:
            raise ApiError(502, "Git failed: " + redact(r.stderr, token).strip()[:500])
        root, prefix = Path(tmp), f"{group}/"
        local = {prefix + rel: p for rel, p in repo_files(root).items()}
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
        raise ApiError(422, f"Invalid {what} name")
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
        pod = (getattr(e.resource, "labels", None) or {}).get("pod_name", "-")
        ts = e.timestamp.isoformat(timespec="seconds") if e.timestamp else None
        doc = e.payload if isinstance(e.payload, dict) else _try_json(e.payload)
        if doc is None:
            # not a worker JSON line (startup banner, sidecar noise) — kept verbatim, logview's fallback path
            # renders it read-only rather than corrupting it with a prefix that only looks like JSON
            lines.append(e.payload if isinstance(e.payload, str) else json.dumps(e.payload))
            continue
        doc.setdefault("ts", ts)
        doc.setdefault("pod", pod)
        lines.append(json.dumps(doc))
    return "\n".join(lines) + ("\n" if lines else "")


def _try_json(text) -> dict | None:
    if not isinstance(text, str):
        return None
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return None
    return doc if isinstance(doc, dict) else None


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

    @staticmethod
    def _later(e) -> bool:
        """A 400 that means "come back later": the resource is still referenced, or (Gateway-managed backend
        services, for minutes after a change) "is not ready"."""
        msg = str(e).lower()
        return http_status(e) == 400 and ("in use" in msg or "being used" in msg or "not ready" in msg)

    def delete_negs(self, name, attempts=45, delay=20.0) -> list[str]:
        """D69: delete every zonal network endpoint group called `name` (the NEG controller names them after the
        zone's namespace). GCP refuses one a backend service still uses, and the Gateway controller keeps that
        backend service for minutes after the route went: retry `attempts` times, `delay` apart. The zones deleted."""
        page = (
            self.api.networkEndpointGroups()
            .aggregatedList(project=self.project, filter=f"name={name}")
            .execute(num_retries=3, http=fresh_http())
        )
        zones = sorted(
            scope.rsplit("/", 1)[-1]
            for scope, v in (page.get("items") or {}).items()
            if any(g.get("name") == name for g in v.get("networkEndpointGroups") or [])
        )
        deleted = []
        # The Gateway controller drops the backend service from its URL map at once but may never delete it; GCE
        # refuses to delete one a URL map still references, so trying is safe — a 400 "in use" means wait.
        for i in range(attempts):
            try:
                bs = self.find_backend_service(name, "").get("name") if zones else None
            except ApiError as e:  # none left: the controller did collect it
                if e.status_code != 404:
                    raise
                bs = None
            if not bs:
                break
            try:
                self._wait(
                    self.api.backendServices()
                    .delete(project=self.project, backendService=bs)
                    .execute(num_retries=3, http=fresh_http())
                )
                break
            except Exception as e:  # noqa: BLE001
                if http_status(e) == 404:
                    break
                if self._later(e) and i < attempts - 1:
                    time.sleep(delay)
                    continue
                raise
        for z in zones:
            for i in range(attempts):
                try:
                    op = (
                        self.api.networkEndpointGroups()
                        .delete(project=self.project, zone=z, networkEndpointGroup=name)
                        .execute(num_retries=3, http=fresh_http())
                    )
                    if isinstance(op, dict) and op.get("name") and op.get("status") != "DONE":
                        self.api.zoneOperations().wait(project=self.project, zone=z, operation=op["name"]).execute(
                            num_retries=3, http=fresh_http()
                        )
                    deleted.append(z)
                    break
                except Exception as e:  # noqa: BLE001
                    if http_status(e) == 404:
                        break
                    if self._later(e) and i < attempts - 1:
                        time.sleep(delay)
                        continue
                    raise
        return deleted

    def delete_armor(self, name, attempts=24) -> bool:
        """Delete policy `name`; False when it does not exist. GCP refuses a policy still set on a backend service,
        and Gateway-managed ones outlive their namespace for minutes: those are cleared first."""
        suffix = f"/securityPolicies/{name}"
        token = None
        while True:
            page = (
                self.api.backendServices()
                .list(project=self.project, pageToken=token)
                .execute(num_retries=3, http=fresh_http())
            )
            for bs in page.get("items", []):
                if str(bs.get("securityPolicy") or "").endswith(suffix):
                    self._ready(
                        lambda b=bs: self.api.backendServices().setSecurityPolicy(
                            project=self.project, backendService=b["name"], body={}
                        ),
                        attempts=attempts,
                    )
            token = page.get("nextPageToken")
            if not token:
                break
        try:
            self._ready(lambda: self.api.securityPolicies().delete(project=self.project, securityPolicy=name), attempts)
        except Exception as e:  # noqa: BLE001
            if http_status(e) == 404:
                return False
            raise
        return True

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

    @staticmethod
    def _remove(policy, roles: set[str], member) -> list[str]:
        """Drop `member` from every binding for `roles`, whatever the condition, and shed emptied bindings (§13.2)."""
        changed = []
        for b in policy.get("bindings", []):
            if b["role"] in roles and member in b["members"]:
                b["members"] = [m for m in b["members"] if m != member]
                changed.append(b["role"])
        if changed:
            policy["bindings"] = [b for b in policy["bindings"] if b["members"]]
        return changed

    # resource-level bindings (SEC-08): the console holds no project-level setIamPolicy -------------------------
    @staticmethod
    def _shed_deleted(policy: dict) -> bool:
        """Drop `deleted:` members (a principal deleted while still bound). GCS refuses a policy write that carries
        one once a principal of that name exists again, for every group that shares the bucket."""
        before = sum(len(b["members"]) for b in policy.get("bindings", []))
        for b in policy.get("bindings", []):
            b["members"] = [m for m in b["members"] if not m.startswith("deleted:")]
        policy["bindings"] = [b for b in policy.get("bindings", []) if b["members"]]
        return before != sum(len(b["members"]) for b in policy["bindings"])

    def purge_bucket_member(self, storage, bucket_name, email) -> bool:
        """Remove the identity from every binding of the bucket, before the identity itself is deleted."""
        bucket = storage.bucket(bucket_name)
        pol = bucket.get_iam_policy(requested_policy_version=3)
        doc = {"bindings": [dict(b) for b in pol.bindings]}
        member = f"serviceAccount:{email}"
        had = any(member in b["members"] for b in doc["bindings"])
        for b in doc["bindings"]:
            b["members"] = [m for m in b["members"] if m != member]
        shed = self._shed_deleted(doc)  # also sheds emptied bindings
        if had or shed:
            pol.version = 3
            pol.bindings = doc["bindings"]
            bucket.set_iam_policy(pol)
        return had

    def grant_bucket_roles(self, storage, bucket_name, email, bindings: list[tuple[str, dict | None]]) -> list[str]:
        """Bind roles on the groups bucket itself (conditions keep them to the group's prefix)."""
        bucket = storage.bucket(bucket_name)
        pol = bucket.get_iam_policy(requested_policy_version=3)
        doc = {"bindings": [dict(b) for b in pol.bindings]}
        self._shed_deleted(doc)
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

    def revoke_bucket_roles(self, storage, bucket_name, email, roles: list[str]) -> list[str]:
        """Unbind the member from those roles on the groups bucket. Returns the roles that were actually bound."""
        bucket = storage.bucket(bucket_name)
        pol = bucket.get_iam_policy(requested_policy_version=3)
        doc = {"bindings": [dict(b) for b in pol.bindings]}
        self._shed_deleted(doc)
        changed = self._remove(doc, set(roles), f"serviceAccount:{email}")
        if changed:
            pol.version = 3
            pol.bindings = doc["bindings"]
            bucket.set_iam_policy(pol)
        return changed

    def revoke_secret_roles(self, sm, group, email, roles: list[str]) -> list[str]:
        changed: list[str] = []
        for name in self.group_secrets(sm, group):
            changed += [r for r in self.unbind_secret(sm, name, roles, [f"serviceAccount:{email}"]) if r not in changed]
        return changed

    @staticmethod
    def unbind_secret(sm, name, roles: list[str], members: list[str]) -> list[str]:
        """Remove `members` from `roles` on one secret; [] when nothing changed or the secret is gone."""
        try:
            pol = sm.get_iam_policy(request={"resource": name})
        except Exception as e:  # noqa: BLE001
            if http_status(e) == 404:
                return []
            raise
        get = lambda b, k: b[k] if isinstance(b, dict) else getattr(b, k)  # noqa: E731 - proto message or dict
        doc = [{"role": get(b, "role"), "members": list(get(b, "members"))} for b in pol.bindings]
        changed = []
        for b in doc:
            if b["role"] in roles:
                keep = [m for m in b["members"] if m not in members]
                if len(keep) != len(b["members"]):
                    b["members"] = keep
                    changed.append(b["role"])
        if changed:
            from google.iam.v1 import policy_pb2

            del pol.bindings[:]
            pol.bindings.extend(policy_pb2.Binding(role=b["role"], members=b["members"]) for b in doc if b["members"])
            sm.set_iam_policy(request={"resource": name, "policy": pol})
        return changed

    def revoke_project_roles(self, email, roles: list[str]) -> list[str]:
        pol = (
            self.crm.projects()
            .getIamPolicy(resource=self.project, body={"options": {"requestedPolicyVersion": 3}})
            .execute(num_retries=3, http=fresh_http())
        )
        pol["version"] = 3
        changed = self._remove(pol, set(roles), f"serviceAccount:{email}")
        if changed:
            self.crm.projects().setIamPolicy(resource=self.project, body={"policy": pol}).execute(
                num_retries=3, http=fresh_http()
            )
        return changed

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
            # the same IAM lag as the bucket grant: a GSA created seconds ago is 400 "… does not exist" for a while
            # (0.8.0, found by an MCP key created right after its zone was attached on GKE)
            for attempt in range(12):
                try:
                    sm.set_iam_policy(request={"resource": name, "policy": pol})
                    break
                except Exception as e:  # noqa: BLE001
                    if http_status(e) != 400 or "does not exist" not in str(e) or attempt == 11:
                        raise
                    time.sleep(min(2**attempt, 10))
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


class Registry:
    """§38.2 (D85): the per-group Artifact Registry repositories `ramen-groups-<group>` and their writer binding. The
    console holds these rights project-wide (Artifact Registry ignores IAM Conditions on the repository name; CONTRACTS
    §38.2 residual) and no upload right; a group's build identity is the only writer of its own repository."""

    def __init__(self, ar, project, location, attempts=10, delay=2.0):
        self.ar, self.project, self.location, self.attempts, self.delay = ar, project, location, attempts, delay

    def _name(self, repo_id) -> str:
        return f"projects/{self.project}/locations/{self.location}/repositories/{repo_id}"

    def _repos(self):
        return self.ar.projects().locations().repositories()

    def get(self, repo_id, missing=(404,)) -> dict | None:
        try:
            return self._repos().get(name=self._name(repo_id)).execute(num_retries=3, http=fresh_http())
        except Exception as e:  # noqa: BLE001
            if http_status(e) not in missing:
                raise
            return None

    def ensure(self, repo_id, labels: dict[str, str], description: str) -> bool:
        """Create the DOCKER repository unless it exists; wait until it answers (creation is an operation). True when
        this call created it. Artifact Registry answers 403, not 404, for a repository that does not exist yet when the
        caller's get right is conditioned on the repository name (GKE, 1.0.0c) — so 403 means "try to create"; a real
        refusal then surfaces as the create's own error."""
        if self.get(repo_id, missing=(403, 404)) is not None:
            return False
        body = {"format": "DOCKER", "labels": labels, "description": description}
        try:
            self._repos().create(
                parent=f"projects/{self.project}/locations/{self.location}", repositoryId=repo_id, body=body
            ).execute(num_retries=3, http=fresh_http())
        except Exception as e:  # noqa: BLE001
            if http_status(e) != 409:
                raise
            return False
        for _ in range(self.attempts):
            if self.get(repo_id, missing=(403, 404)) is not None:
                break
            time.sleep(self.delay)
        return True

    def bind(self, repo_id, role, member) -> bool:
        res, repos = self._name(repo_id), self._repos()
        pol = repos.getIamPolicy(resource=res).execute(num_retries=3, http=fresh_http())
        if not Iam._add(pol, role, member):
            return False
        repos.setIamPolicy(resource=res, body={"policy": pol}).execute(num_retries=3, http=fresh_http())
        return True

    def delete(self, repo_id) -> bool:
        try:
            self._repos().delete(name=self._name(repo_id)).execute(num_retries=3, http=fresh_http())
            return True
        except Exception as e:  # noqa: BLE001
            if http_status(e) != 404:
                raise
            return False
