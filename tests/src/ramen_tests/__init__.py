"""Ramen cross-component test helpers. Every helper takes a URL/target so suites run locally or in cloud."""

import os

# Read by grpc's C-core at import time. With fork support on, its fork handler logs an INFO line onto the stderr of
# every child this process forks (the sidecar under test_sidecar_protocol), breaking "stderr is JSON lines".
# The harness never uses grpc across a fork, so the handlers are not needed.
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")
