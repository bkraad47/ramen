"""In-memory fakes for kubernetes, GCS, Secret Manager, Cloud Logging and discovery-style compute/iam/crm clients."""
import base64
import copy
import hashlib
import json
from types import SimpleNamespace as NS


class FakeApiError(Exception):
    def __init__(self, status, msg=""):
        super().__init__(msg or f"http {status}")
        self.status = status


class FakeK8s:
    """Shared state; core/apps/autoscaling views expose the subset of the kubernetes client the adapter uses."""

    def __init__(self):
        self.objs: dict[tuple, dict] = {}
        self.ready = True
        self.calls: list[tuple] = []

    # generic helpers ---------------------------------------------------
    def _key(self, kind, body, ns=None):
        m = body["metadata"]
        return (kind, ns or m.get("namespace"), m["name"])

    def _create(self, kind, ns, body):
        k = self._key(kind, body, ns)
        self.calls.append(("create", *k))
        if k in self.objs:
            raise FakeApiError(409, "AlreadyExists")
        self.objs[k] = copy.deepcopy(body)
        return copy.deepcopy(body)

    def _patch(self, kind, ns, name, body):
        k = (kind, ns, name)
        self.calls.append(("patch", *k))
        if k not in self.objs:
            raise FakeApiError(404, "NotFound")
        _merge(self.objs[k], copy.deepcopy(body))
        return copy.deepcopy(self.objs[k])

    def _read(self, kind, ns, name):
        k = (kind, ns, name)
        if k not in self.objs:
            raise FakeApiError(404, "NotFound")
        obj = copy.deepcopy(self.objs[k])
        if kind == "Deployment":
            reps = obj["spec"].get("replicas", 1)
            ready = reps if self.ready else 0
            obj["status"] = {"replicas": reps, "readyReplicas": ready, "updatedReplicas": ready,
                             "availableReplicas": ready, "observedGeneration": 1}
            obj["metadata"]["generation"] = 1
        return obj

    def pods(self, ns, selector=""):
        want = dict(kv.split("=") for kv in selector.split(",") if kv)
        out = []
        for (kind, n, name), d in self.objs.items():
            if kind != "Deployment" or n != ns:
                continue
            labels = d["spec"]["template"]["metadata"]["labels"]
            if any(labels.get(k) != v for k, v in want.items()):
                continue
            for i in range(d["spec"].get("replicas", 1)):
                ip = f"10.{1 if labels.get('ramen.io/track') == 'canary' else 2}.0.{i + 1}"
                out.append({"metadata": {"name": f"{name}-{i}", "labels": labels, "namespace": ns},
                            "status": {"phase": "Running" if self.ready else "Pending", "podIP": ip}})
        return out

    @property
    def core(self):
        return _Core(self)

    @property
    def apps(self):
        return _Apps(self)

    @property
    def autoscaling(self):
        return _Hpa(self)

    @property
    def custom(self):
        return _Custom(self)


class _Custom:
    def __init__(self, s):
        self.s = s

    def create_namespaced_custom_object(self, group, version, ns, plural, body):
        return self.s._create(body["kind"], ns, body)

    def patch_namespaced_custom_object(self, group, version, ns, plural, name, body):
        return self.s._patch(body["kind"], ns, name, body)

    def get_namespaced_custom_object(self, group, version, ns, plural, name):
        return self.s._read("HTTPRoute", ns, name)


def _merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


class _Core:
    def __init__(self, s):
        self.s = s

    def create_namespace(self, body):
        return self.s._create("Namespace", None, body)

    def patch_namespace(self, name, body):
        return self.s._patch("Namespace", None, name, body)

    def read_namespace(self, name):
        return self.s._read("Namespace", None, name)

    def list_namespace(self, label_selector=""):
        key = label_selector.split("=")[0]
        return {"items": [copy.deepcopy(v) for (k, _, _), v in self.s.objs.items()
                          if k == "Namespace" and key in v["metadata"].get("labels", {})]}

    def create_namespaced_service_account(self, ns, body):
        return self.s._create("ServiceAccount", ns, body)

    def patch_namespaced_service_account(self, name, ns, body):
        return self.s._patch("ServiceAccount", ns, name, body)

    def read_namespaced_service_account(self, name, ns):
        return self.s._read("ServiceAccount", ns, name)

    def create_namespaced_secret(self, ns, body):
        return self.s._create("Secret", ns, body)

    def patch_namespaced_secret(self, name, ns, body):
        return self.s._patch("Secret", ns, name, body)

    def read_namespaced_secret(self, name, ns):
        return self.s._read("Secret", ns, name)

    def create_namespaced_service(self, ns, body):
        return self.s._create("Service", ns, body)

    def patch_namespaced_service(self, name, ns, body):
        return self.s._patch("Service", ns, name, body)

    def list_namespaced_pod(self, ns, label_selector=""):
        return {"items": self.s.pods(ns, label_selector)}

    def connect_get_namespaced_pod_proxy_with_path(self, name, ns, path, **kw):
        self.s.calls.append(("proxy", ns, name, path))
        return json.dumps({"inflight": 0, "total": 1, "errors": 0, "load": "low", "via": "proxy"})


class _Apps:
    def __init__(self, s):
        self.s = s

    def create_namespaced_deployment(self, ns, body):
        return self.s._create("Deployment", ns, body)

    def patch_namespaced_deployment(self, name, ns, body):
        return self.s._patch("Deployment", ns, name, body)

    def read_namespaced_deployment(self, name, ns):
        return self.s._read("Deployment", ns, name)

    def list_namespaced_deployment(self, ns):
        return {"items": [self.s._read("Deployment", ns, n) for (k, s_, n) in list(self.s.objs) if k == "Deployment" and s_ == ns]}


class _Hpa:
    def __init__(self, s):
        self.s = s

    def create_namespaced_horizontal_pod_autoscaler(self, ns, body):
        return self.s._create("HorizontalPodAutoscaler", ns, body)

    def patch_namespaced_horizontal_pod_autoscaler(self, name, ns, body):
        return self.s._patch("HorizontalPodAutoscaler", ns, name, body)

    def read_namespaced_horizontal_pod_autoscaler(self, name, ns):
        return self.s._read("HorizontalPodAutoscaler", ns, name)


# GCS ------------------------------------------------------------------
class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    @property
    def md5_hash(self):
        return base64.b64encode(hashlib.md5(self.bucket.data[self.name]).digest()).decode()

    def upload_from_filename(self, path):
        self.bucket.data[self.name] = open(path, "rb").read()

    def delete(self):
        self.bucket.data.pop(self.name, None)


class FakeBucket:
    def __init__(self):
        self.data: dict[str, bytes] = {}

    def list_blobs(self, prefix=""):
        return [FakeBlob(self, n) for n in sorted(self.data) if n.startswith(prefix)]

    def blob(self, name):
        return FakeBlob(self, name)


class FakeStorage:
    def __init__(self):
        self.buckets: dict[str, FakeBucket] = {}

    def bucket(self, name):
        return self.buckets.setdefault(name, FakeBucket())


# Secret Manager --------------------------------------------------------
class FakeSM:
    def __init__(self):
        self.secrets: dict[str, dict] = {}
        self.calls: list[str] = []

    def create_secret(self, request):
        name = f"{request['parent']}/secrets/{request['secret_id']}"
        self.calls.append("create")
        if name in self.secrets:
            raise FakeApiError(409, "already exists")
        self.secrets[name] = {"labels": dict(request["secret"].get("labels", {})), "versions": []}
        return NS(name=name)

    def add_secret_version(self, request):
        self.calls.append("add_version")
        self.secrets[request["parent"]]["versions"].append(request["payload"]["data"])
        return NS(name=f"{request['parent']}/versions/{len(self.secrets[request['parent']]['versions'])}")

    def access_secret_version(self, request):
        base, _, _ = request["name"].rpartition("/versions/")
        if base not in self.secrets or not self.secrets[base]["versions"]:
            raise FakeApiError(404, "not found")
        return NS(payload=NS(data=self.secrets[base]["versions"][-1]))

    def delete_secret(self, request):
        self.calls.append("delete")
        if request["name"] not in self.secrets:
            raise FakeApiError(404, "not found")
        del self.secrets[request["name"]]


# Cloud Logging ---------------------------------------------------------
class FakeLogging:
    def __init__(self, entries=None):
        self.entries = entries or []
        self.filters: list[str] = []

    def list_entries(self, filter_="", order_by="", max_results=None, page_size=None):
        self.filters.append(filter_)
        return iter(self.entries[:max_results])


# discovery-style (compute / iam / cloudresourcemanager) -------------------
class FakeDiscovery:
    """handler(collection, method, kwargs) -> response; collections chain through SUB names."""
    SUB = {"serviceAccounts"}

    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def __getattr__(self, coll):
        return lambda: _Coll(self, coll)


class _Coll:
    def __init__(self, d, name):
        self.d, self.name = d, name

    def __getattr__(self, method):
        if method in FakeDiscovery.SUB:
            return lambda: _Coll(self.d, f"{self.name}.{method}")

        def call(**kw):
            self.d.calls.append((self.name, method, kw))
            return NS(execute=lambda: self.d.handler(self.name, method, kw))
        return call


def compute_handler(state):
    """state: {'backend': {...} | None, 'policies': {name: policy}}"""
    def h(coll, method, kw):
        if coll == "backendServices":
            if method == "list":
                if kw.get("pageToken") is None and state.get("paged"):
                    return {"items": [{"name": "other", "backends": []}], "nextPageToken": "p2"}
                return {"items": [copy.deepcopy(state["backend"])] if state.get("backend") else []}
            if state.get("backend") is None or kw["backendService"] != state["backend"]["name"]:
                raise FakeApiError(404, "backend service not found")
            if method == "get":
                return copy.deepcopy(state["backend"])
            if method == "patch":
                state["backend"].update(kw["body"])
                return {"status": "DONE"}
            if method == "setSecurityPolicy":
                state["backend"]["securityPolicy"] = kw["body"]["securityPolicy"]
                return {"status": "DONE"}
        if coll == "securityPolicies":
            pols = state.setdefault("policies", {})
            if method == "get":
                if kw["securityPolicy"] not in pols:
                    raise FakeApiError(404, "policy not found")
                return copy.deepcopy(pols[kw["securityPolicy"]])
            if method == "insert":
                pols[kw["body"]["name"]] = copy.deepcopy(kw["body"])
                return {"status": "DONE"}
            p = pols[kw["securityPolicy"]]
            if method == "addRule":
                p["rules"].append(kw["body"])
            if method == "removeRule":
                p["rules"] = [r for r in p["rules"] if r["priority"] != kw["priority"]]
            if method == "patchRule":
                for r in p["rules"]:
                    if r["priority"] == kw["priority"]:
                        r.update(kw["body"])
            return {"status": "DONE"}
        raise AssertionError(f"unexpected {coll}.{method}")
    return h


def iam_handler(state):
    """state: {'accounts': {email: {...}}, 'sa_policy': {email: policy}, 'project_policy': policy, 'number': '123'}"""
    def h(coll, method, kw):
        if coll == "projects.serviceAccounts":
            accts = state.setdefault("accounts", {})
            if method == "create":
                project = kw["name"].split("/")[1]
                email = f"{kw['body']['accountId']}@{project}.iam.gserviceaccount.com"
                if email in accts:
                    raise FakeApiError(409, "exists")
                accts[email] = {"email": email, "name": f"{kw['name']}/serviceAccounts/{email}"}
                return accts[email]
            if method == "get":
                email = kw["name"].rsplit("/", 1)[1]
                if email not in accts:
                    raise FakeApiError(404, "no sa")
                return accts[email]
            if method == "list":
                return {"accounts": list(accts.values())}
            email = kw["resource"].rsplit("/", 1)[1]
            if method == "getIamPolicy":
                return copy.deepcopy(state.setdefault("sa_policy", {}).get(email, {"bindings": []}))
            if method == "setIamPolicy":
                state.setdefault("sa_policy", {})[email] = copy.deepcopy(kw["body"]["policy"])
                return kw["body"]["policy"]
        if coll == "projects":
            if method == "get":
                return {"projectNumber": state.get("number", "123456")}
            if method == "getIamPolicy":
                return copy.deepcopy(state.setdefault("project_policy", {"bindings": [], "version": 3}))
            if method == "setIamPolicy":
                state["project_policy"] = copy.deepcopy(kw["body"]["policy"])
                return kw["body"]["policy"]
        raise AssertionError(f"unexpected {coll}.{method}")
    return h


class FakeClients:
    def __init__(self, k8s=None, storage=None, sm=None, logging=None, compute_state=None, iam_state=None):
        self.k8s = k8s or FakeK8s()
        self.storage = storage or FakeStorage()
        self.secretmanager = sm or FakeSM()
        self.logging = logging or FakeLogging()
        self.compute_state = compute_state if compute_state is not None else {"backend": None, "policies": {}}
        self.iam_state = iam_state if iam_state is not None else {}
        self.compute = FakeDiscovery(compute_handler(self.compute_state))
        self.iam = FakeDiscovery(iam_handler(self.iam_state))
        self.crm = self.iam

    @property
    def core(self):
        return self.k8s.core

    @property
    def apps(self):
        return self.k8s.apps

    @property
    def autoscaling(self):
        return self.k8s.autoscaling

    @property
    def custom(self):
        return self.k8s.custom

    def to_dict(self, obj):
        return obj
