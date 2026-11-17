"""Lazy real GCP/Kubernetes clients; tests inject a fake bundle with the same attribute names."""

import functools


def http_status(e: BaseException) -> int | None:
    """Status of a kubernetes ApiException (.status), google api_core error (.code)
    or googleapiclient HttpError (.resp.status)."""
    for attr in ("status", "code"):
        v = getattr(e, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(e, "resp", None)
    v = getattr(resp, "status", None)
    return int(v) if isinstance(v, (int, str)) and str(v).isdigit() else None


def _dropped(error) -> bool:
    """urllib3's `ProtocolError("Connection aborted.", <cause>)` for a connection closed before any answer came back."""
    import http.client

    import urllib3

    return isinstance(error, urllib3.exceptions.ProtocolError) and any(
        isinstance(a, (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError)) for a in error.args
    )


@functools.cache
def retry_class():
    """`StaleConnectionRetry`, built on first use: urllib3 comes with the kubernetes extra."""
    from urllib3.util.retry import Retry

    class StaleConnectionRetry(Retry):
        """urllib3 resends a request after a read error only for idempotent methods. A pooled connection the API server
        closed while idle (AKS: the first write of a deploy, minutes after the last call) fails the next POST/PATCH with
        RemoteDisconnected before any answer, so that error is resent for every method; a timeout still never is."""

        def increment(self, method=None, url=None, response=None, error=None, _pool=None, _stacktrace=None):
            if response is None and _dropped(error):
                method = "GET"  # unanswered on a dropped connection: urllib3's own rule for a resendable request
            return super().increment(method, url, response, error, _pool, _stacktrace)

    return StaleConnectionRetry


def kube_retry():
    """The Retry of every Kubernetes client the console builds: urllib3's `Retry(3)`, resending dropped writes."""
    return retry_class()(3)


def _creds():
    """Optional: GOOGLE_OAUTH_ACCESS_TOKEN (e.g. `gcloud auth print-access-token`) when no ADC is available."""
    import os

    token = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
    if not token:
        return None
    from google.oauth2.credentials import Credentials

    return Credentials(token)


class GcpClients:
    def __init__(self, project: str):
        self.project = project
        self._c: dict = {}

    def _kube_modules(self):
        from kubernetes import client, config

        return config, client

    def _kube(self):
        if "kube" not in self._c:
            cfg, client = self._kube_modules()
            try:
                cfg.load_incluster_config()
            except Exception:  # noqa: BLE001 - not in a pod: fall back to KUBECONFIG / ~/.kube/config
                cfg.load_kube_config()
            default = client.Configuration.get_default_copy()
            default.retries = kube_retry()  # every `client.<Api>()` below copies the default configuration
            client.Configuration.set_default(default)
            self._c["kube"] = client
        return self._c["kube"]

    def _cached(self, key, make):
        if key not in self._c:
            self._c[key] = make()
        return self._c[key]

    @property
    def core(self):
        return self._cached("core", lambda: self._kube().CoreV1Api())

    @property
    def apps(self):
        return self._cached("apps", lambda: self._kube().AppsV1Api())

    @property
    def batch(self):
        return self._cached("batch", lambda: self._kube().BatchV1Api())

    @property
    def autoscaling(self):
        return self._cached("hpa", lambda: self._kube().AutoscalingV2Api())

    @property
    def custom(self):
        return self._cached("custom", lambda: self._kube().CustomObjectsApi())

    @property
    def networking(self):
        return self._cached("networking", lambda: self._kube().NetworkingV1Api())

    @property
    def rbac(self):
        return self._cached("rbac", lambda: self._kube().RbacAuthorizationV1Api())

    def to_dict(self, obj):
        if isinstance(obj, dict):
            return obj
        api = self._cached("api_client", lambda: self._kube_modules()[1].ApiClient())
        return api.sanitize_for_serialization(obj)

    @property
    def storage(self):
        def make():
            from google.cloud import storage

            return storage.Client(project=self.project, credentials=_creds())

        return self._cached("storage", make)

    @property
    def secretmanager(self):
        def make():
            from google.cloud import secretmanager

            return secretmanager.SecretManagerServiceClient(credentials=_creds())

        return self._cached("sm", make)

    @property
    def logging(self):
        def make():
            from google.cloud import logging as gcl

            return gcl.Client(project=self.project, credentials=_creds())

        return self._cached("logging", make)

    def _discovery(self, name, version):
        def make():
            from googleapiclient.discovery import build

            return build(name, version, cache_discovery=False, credentials=_creds())

        return self._cached(name, make)

    @property
    def compute(self):
        return self._discovery("compute", "v1")

    @property
    def iam(self):
        return self._discovery("iam", "v1")

    @property
    def crm(self):
        return self._discovery("cloudresourcemanager", "v1")

    @property
    def artifactregistry(self):  # §38.2: per-group repositories
        return self._discovery("artifactregistry", "v1")

    def for_cluster(self, project, location, name):
        """§28.2: a satellite GKE cluster's clients — kubernetes bound to its API server, the rest this bundle's."""
        return GkeClients(self, project, location, name)


class _Apis:
    """`client.<SomeApi>()` bound to one ApiClient, so the inherited `core`/`apps`/... properties work unchanged."""

    def __init__(self, module, api_client):
        self.m, self.api = module, api_client

    def __getattr__(self, name):
        cls = getattr(self.m, name)
        return lambda: cls(self.api)


def remote_kube(client, endpoint: str, ca_pem: bytes, token_fn) -> _Apis:
    """A kubernetes client for a remote API server: its CA, a bearer token from `token_fn` refreshed before calls."""
    import tempfile

    cfg = client.Configuration()
    cfg.host = endpoint if endpoint.startswith("https://") else f"https://{endpoint}"
    with tempfile.NamedTemporaryFile(delete=False, suffix=".crt") as f:
        f.write(ca_pem)
    cfg.ssl_ca_cert = f.name

    def refresh(c):  # the prefix table is not applied by every client version (36: bare token → 401): store it whole
        c.api_key["authorization"] = f"Bearer {token_fn()}"

    cfg.refresh_api_key_hook = refresh
    cfg.retries = kube_retry()
    refresh(cfg)
    return _Apis(client, client.ApiClient(cfg))


def google_token() -> str:
    """The console's Google access token (ADC in the pod, `GOOGLE_OAUTH_ACCESS_TOKEN` locally), refreshed when stale."""
    creds = _adc()
    if creds is None:
        raise RuntimeError("no Google credentials for the satellite cluster (ADC or GOOGLE_OAUTH_ACCESS_TOKEN)")
    if not creds.valid:
        from google.auth.transport.requests import Request

        creds.refresh(Request())
    return creds.token


class GkeClients(GcpClients):
    """A satellite GKE cluster's clients (§28.2): endpoint and CA from `container.projects.locations.clusters.get`,
    the console GSA's token as bearer (the satellite chart binds `User <gsa email>` to the console ClusterRoles)."""

    def __init__(self, home: GcpClients, project: str, location: str, name: str):
        super().__init__(project)
        self.home, self.location, self.name = home, location, name

    def cluster_info(self) -> dict:
        import base64

        path = f"projects/{self.project}/locations/{self.location}/clusters/{self.name}"
        info = self.home._discovery("container", "v1").projects().locations().clusters().get(name=path).execute()
        return {"endpoint": info["endpoint"], "ca": base64.b64decode(info["masterAuth"]["clusterCaCertificate"])}

    def _kube(self):
        if "kube" not in self._c:
            info = self.cluster_info()
            self._c["kube"] = remote_kube(self._kube_modules()[1], info["endpoint"], info["ca"], google_token)
        return self._c["kube"]

    # the project-level planes are the home bundle's (one set of credentials and discovery clients)
    storage = property(lambda self: self.home.storage)
    secretmanager = property(lambda self: self.home.secretmanager)
    logging = property(lambda self: self.home.logging)
    compute = property(lambda self: self.home.compute)
    iam = property(lambda self: self.home.iam)
    crm = property(lambda self: self.home.crm)


def fresh_http():
    """A per-call authorized httplib2 transport. googleapiclient's shared Http is not thread-safe; using one
    connection from two threads (e.g. a background Cloud Armor attach and a rebalance) segfaults in OpenSSL."""
    import os

    if os.environ.get("RAMEN_GCP_FRESH_HTTP", "1") == "0":
        return None
    try:
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp

        creds = _adc()
        return AuthorizedHttp(creds, http=httplib2.Http(timeout=120)) if creds is not None else None
    except Exception:  # noqa: BLE001 - no credentials: let the client use its default transport
        return None


_ADC: list = []


def _adc():
    """Explicit token if given, else Application Default Credentials (Workload Identity in-cluster), cached."""
    creds = _creds()
    if creds is not None:
        return creds
    if not _ADC:  # probe once; a failure is cached too so fakes/tests never pay for metadata-server timeouts twice
        try:
            import google.auth

            _ADC.append(google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])[0])
        except Exception:  # noqa: BLE001
            _ADC.append(None)
    return _ADC[0]
