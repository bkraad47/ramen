"""A Cloud for the §24 console tests: the local adapter plus recorded sleep / wake / build calls (the real adapters
have their own tests)."""

import asyncio

from ramen_console.cloud.local import LocalCloud

BASE_IMAGE = "reg.example/ramen/worker:1.0.0"


class BuildFailed(RuntimeError):
    def __init__(self, msg, log=""):
        super().__init__(msg)
        self.log = log


class StubCloud(LocalCloud):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.image = BASE_IMAGE
        self.calls: list[tuple] = []
        self.runtime = {"workers": 1, "canary": 0, "activator": 0}
        self.builder = True
        self.build_error: Exception | None = None
        self.build_out: dict | None = None
        self.wake_error: Exception | None = None
        self.sleep_error: Exception | None = None
        self.wake_gate: asyncio.Event | None = None
        self.deploys: list[dict] = []
        self.pushes: list[dict] = []

    async def deploy(self, group, env, zone, canary=True, config=None, spec=None, log=None, gate=None):
        self.deploys.append({"zone": zone, "config": dict(config or {}), "spec": dict(spec or {})})
        return await super().deploy(group, env, zone, canary, config, spec, log, gate)

    async def push_config(self, group, env, zone, config):  # §26.3
        self.pushes.append({"zone": zone, "env": env, "config": dict(config)})
        return await super().push_config(group, env, zone, config)

    async def zone_runtime(self, group, zone):
        return dict(self.runtime)

    async def sleep_zone(self, group, zone):
        if self.sleep_error:
            raise self.sleep_error
        self.calls.append(("sleep", group, zone))
        previous = {"workers": self.runtime["workers"], "canary": self.runtime["canary"]}
        self.runtime = {"workers": 0, "canary": 0, "activator": 1}
        return {"ok": True, "previous": previous, **self.runtime}

    async def wake_zone(self, group, zone, count, canary):
        self.calls.append(("wake", group, zone, count, canary))
        if self.wake_gate is not None:
            await self.wake_gate.wait()
        if self.wake_error:
            raise self.wake_error
        self.runtime = {"workers": count, "canary": canary, "activator": 0}
        return {"ok": True, **self.runtime}

    def builder_available(self) -> bool:
        return self.builder

    async def build_image(self, group, tag, dockerfile, files):
        self.calls.append(("build", group, tag, dockerfile, files))
        if self.build_error:
            raise self.build_error
        return self.build_out or {"image": f"reg.example/ramen/worker:{tag}", "log": "pushed"}
