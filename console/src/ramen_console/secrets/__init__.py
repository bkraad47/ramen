import os

from .base import REF_PREFIX, SecretsBackend, StoreBackend


def make_secrets_backend(cloud=None) -> SecretsBackend:
    kind = os.environ.get("RAMEN_SECRETS_BACKEND", "store")
    if kind == "store":
        return StoreBackend()
    if kind == "gcp":
        from .gcp import GcpSecrets
        project = os.environ.get("RAMEN_GCP_PROJECT")
        if not project:
            raise ValueError("RAMEN_SECRETS_BACKEND=gcp needs RAMEN_GCP_PROJECT")
        client = getattr(getattr(cloud, "c", None), "secretmanager", None)
        if client is None:
            from ..cloud.gcp_clients import GcpClients
            client = GcpClients(project).secretmanager
        return GcpSecrets(project, client)
    raise ValueError(f"unknown RAMEN_SECRETS_BACKEND={kind!r}; use store|gcp")
