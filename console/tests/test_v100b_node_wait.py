"""A rollout whose pods wait for a node (Autopilot or a node pool scaling up for a new zone) gets a longer deadline
(`RAMEN_NODE_WAIT_SECS`, default 1200 s) than one that is merely slow or broken (`RAMEN_DEPLOY_TIMEOUT_SECS`, 300 s).
Found live: a new zone on GKE Autopilot takes 10–18 min to get its first node and the deploy failed at 300 s."""

import time

import pytest

from ramen_console.cloud.gcp_k8s import Kube

UNSCHEDULABLE = {
    "metadata": {"name": "worker-canary-1"},
    "status": {
        "phase": "Pending",
        "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "0/5 nodes"}],
    },
}
CRASHING = {
    "metadata": {"name": "worker-canary-1"},
    "status": {"phase": "Running", "containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]},
}


class FakeKube(Kube):
    def __init__(self, pods, ready_after=None, **kw):
        super().__init__(clients=None, poll=0.01, **kw)
        self._pods, self._ready_at, self._t0 = pods, ready_after, time.monotonic()

    def read(self, kind, ns, name):
        ready = self._ready_at is not None and time.monotonic() - self._t0 >= self._ready_at
        n = 1 if ready else 0
        return {
            "metadata": {"generation": 1},
            "spec": {"replicas": 1},
            "status": {"observedGeneration": 1, "updatedReplicas": 1, "readyReplicas": n, "replicas": 1},
        }

    def pods(self, ns, selector=None):
        return self._pods


def test_pods_waiting_for_a_node_get_the_node_deadline():
    k = FakeKube([UNSCHEDULABLE], ready_after=0.15, wait_secs=0.05, node_wait_secs=1)
    assert k.wait_ready("ramen-demo-b", "worker-canary")["status"]["readyReplicas"] == 1


def test_a_rollout_still_without_a_node_fails_at_the_node_deadline_and_says_so():
    k = FakeKube([UNSCHEDULABLE], wait_secs=0.05, node_wait_secs=0.2)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match=r"not ready after waiting 0\.2s for a node.*Unschedulable"):
        k.wait_ready("ramen-demo-b", "worker-canary")
    assert 0.2 <= time.monotonic() - t0 < 1


def test_a_broken_rollout_still_fails_at_the_deploy_deadline():
    k = FakeKube([CRASHING], wait_secs=0.05, node_wait_secs=5)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match=r"not ready after 0\.05s.*CrashLoopBackOff"):
        k.wait_ready("ramen-demo-b", "worker-canary")
    assert time.monotonic() - t0 < 1


def test_the_node_deadline_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("RAMEN_NODE_WAIT_SECS", "42")
    assert Kube(clients=None).node_wait_secs == 42
    monkeypatch.delenv("RAMEN_NODE_WAIT_SECS")
    assert Kube(clients=None).node_wait_secs == 1200


def test_pod_logs_are_text_whatever_the_client_hands_back():
    # kubernetes-client 36 answered the evals Job's log as the bytes' repr on GKE (1.0.0c): "b'...\\n{...}'" — the
    # markers were found but the JSON between them was escaped text.
    from ramen_console.cloud.gcp_k8s import log_text

    assert log_text(None) == ""
    assert log_text(b"a\nb") == "a\nb"
    assert log_text("plain\ntext") == "plain\ntext"
    assert log_text('b\'line one\\n{"k": "v"}\'') == 'line one\n{"k": "v"}'
    assert log_text('b"x\\ty"') == "x\ty"
    assert log_text("b'unterminated") == "b'unterminated"
