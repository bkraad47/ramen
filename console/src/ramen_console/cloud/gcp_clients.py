"""Lazy real GCP/Kubernetes clients; tests inject a fake bundle with the same attribute names."""


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
    def autoscaling(self):
        return self._cached("hpa", lambda: self._kube().AutoscalingV2Api())

    @property
    def custom(self):
        return self._cached("custom", lambda: self._kube().CustomObjectsApi())

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
