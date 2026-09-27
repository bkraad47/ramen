import os

from .base import Cloud


def make_cloud() -> Cloud:
    kind = os.environ.get("RAMEN_CLOUD", "local")
    if kind == "local":
        from .local import LocalCloud
        return LocalCloud.from_env()
    if kind == "gcp":
        from .gcp import GcpCloud
        return GcpCloud.from_env()
    if kind == "aws":
        from .aws import AwsCloud
        return AwsCloud()
    raise ValueError(f"unknown RAMEN_CLOUD={kind!r}; use local|gcp|aws")
