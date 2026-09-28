import os

from .base import SecretsBackend, StoreBackend


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
        return GcpSecrets(project, client, on_create=getattr(cloud, "bind_new_secret", None))
    if kind == "aws":
        from .aws import from_env

        return from_env(cloud)
    raise ValueError(f"unknown RAMEN_SECRETS_BACKEND={kind!r}; use store|gcp|aws")
