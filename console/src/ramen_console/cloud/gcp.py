from .base import Cloud

MSG = "gcp cloud adapter is not available in v0.1.0 (planned for v0.2.0); use RAMEN_CLOUD=local"


class GcpCloud(Cloud):
    async def sync_repo(self, group, repo_url, ref, token):
        raise NotImplementedError(MSG)

    async def deploy(self, group, env, zone, canary=True, config=None):
        raise NotImplementedError(MSG)

    async def rebalance(self, group, zone):
        raise NotImplementedError(MSG)

    async def workers(self, group, zone):
        raise NotImplementedError(MSG)

    async def logs(self, group, zone, worker=None, tail=500):
        raise NotImplementedError(MSG)

    async def set_ip_rules(self, group, zone, cidrs):
        raise NotImplementedError(MSG)

    async def create_service_account(self, group, zone):
        raise NotImplementedError(MSG)

    async def refresh(self):
        raise NotImplementedError(MSG)
