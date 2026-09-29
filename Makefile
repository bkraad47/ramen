# Ramen v0.5.2 developer entrypoints. Needs: uv, cargo (rust-toolchain.toml), docker compose (+ buildx and gcloud for `push`).
export PATH := /opt/homebrew/opt/rustup/bin:/opt/homebrew/bin:$(HOME)/.cargo/bin:$(PATH)
VERSION := $(shell cat VERSION)
COMPOSE := docker compose -f deploy/local/docker-compose.yml
PROJECT ?=
REGION ?= us-central1
PLATFORM ?= linux/amd64
# Per-group worker images (CONTRACTS §13.3, F9.3): `make build-worker GROUP=demo` tags ramen-worker:<version>-demo,
# `make push-worker GROUP=demo PROJECT=<id>` pushes it. Record the pushed reference in the console to pin it:
#   POST /api/v1/groups/<group>/images {"tag": "<registry>/worker:<tag>"}
GROUP ?=
TAG ?= $(VERSION)$(if $(GROUP),-$(GROUP),)
REGISTRY = $(REGION)-docker.pkg.dev/$(PROJECT)/ramen
.PHONY: proto test test-runtime test-node test-console test-harness lint build build-worker build-console push push-worker push-console auth-docker env up down logs demo demo-worker clean kind-up kind-test kind-down shots

test: test-runtime test-node test-console

# Console screenshots for docs/img (CONTRACTS §15): seeds a throwaway console with fake workers, captures every page
# with playwright, and records docs/img/shots.json. `make shots OUT=reports/ui-<version>` writes elsewhere.
# First run needs the browser: uv run --with playwright playwright install chromium
OUT ?= docs/img
shots:
	cd console && uv run --with playwright --with httpx python ../scripts/shots.py --out $(OUT)

# ruff (shared ruff.toml) on every Python tree, cargo fmt/clippy, helm lint, terraform fmt; actionlint/shellcheck when installed.
lint:
	@for d in console runtime-py tests; do (cd $$d && uv sync -q --all-extras && uv run ruff check . && uv run ruff format --check .) || exit 1; done
	cd console && uv run ruff check ../scripts ../deploy/local && uv run ruff format --check ../scripts ../deploy/local
	cd node-rs && cargo fmt --check && cargo clippy --all-targets -- -D warnings
	helm lint deploy/helm/ramen && helm lint deploy/helm/ramen --set provider=aws && helm lint deploy/helm/ramen-worker && helm lint deploy/helm/ramen-worker --set provider=aws
	terraform -chdir=deploy/terraform/gcp fmt -check && terraform -chdir=deploy/terraform/aws fmt -check
	@command -v actionlint >/dev/null && actionlint || echo "actionlint not installed, skipped"
	@command -v shellcheck >/dev/null && shellcheck scripts/*.sh deploy/scripts/*.sh deploy/local/*.sh console/entrypoint.sh || echo "shellcheck not installed, skipped"

test-runtime:
	cd runtime-py && uv sync -q --all-extras && uv run pytest -q --cov --cov-fail-under=90

test-node:
	cd node-rs && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test

test-console:
	@if [ -f console/pyproject.toml ] && ls console/tests/*.py >/dev/null 2>&1; then cd console && uv sync -q --all-extras && uv run pytest -q --cov --cov-fail-under=90; else echo "console: no tests yet"; fi

test-harness:
	cd tests && uv sync -q && uv run pytest -q

build: build-worker build-console

build-worker:
	docker build -f node-rs/Dockerfile --build-arg VERSION=$(VERSION) -t ramen-worker:$(TAG) .
	@echo "built ramen-worker:$(TAG)$(if $(GROUP), for group $(GROUP),)"

build-console:
	@if [ -f console/Dockerfile ]; then docker build -t ramen-console:$(VERSION) console; else echo "console/Dockerfile missing"; fi

# GCP (CONTRACTS §7): linux/amd64 images pushed to Artifact Registry as <region>-docker.pkg.dev/<project>/ramen/{worker,console}:<VERSION>.
#   make push PROJECT=<id> REGION=us-central1      (repo `ramen` is created by deploy/terraform/gcp)
push: auth-docker push-worker push-console

auth-docker:
	@test -n "$(PROJECT)" || { echo "PROJECT=<gcp project> required"; exit 2; }
	gcloud auth configure-docker $(REGION)-docker.pkg.dev --quiet

push-worker:
	docker buildx build --platform $(PLATFORM) -f node-rs/Dockerfile --build-arg VERSION=$(VERSION) -t $(REGISTRY)/worker:$(TAG) --push .
	@echo "pushed $(REGISTRY)/worker:$(TAG)$(if $(GROUP), — record it on group $(GROUP) to pin it,)"

push-console:
	docker buildx build --platform $(PLATFORM) --build-arg RAMEN_VERSION=$(VERSION) -t $(REGISTRY)/console:$(VERSION) --push console

env:
	@[ -f deploy/local/.env ] || cp deploy/local/.env.example deploy/local/.env
	@[ deploy/local/.env -nt deploy/local/.env.example ] || echo "note: deploy/local/.env is older than .env.example — new keys may be missing (diff them or delete .env to regenerate)"

up: env
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down -v

logs:
	$(COMPOSE) logs -f --tail=100

# Full path: stack up → console API creates group demo → deploy → MCP call (needs the console).
demo: up
	@for i in $$(seq 1 90); do curl -sk -o /dev/null https://localhost:8443/login && [ "$$($(COMPOSE) ps --format '{{.Health}}' worker)" = healthy ] && break; sleep 2; done
	deploy/local/demo.sh

# Worker-only path: local ramen-node + runtime-py against a clone of the demo repo, no console/docker.
demo-worker:
	deploy/local/demo-worker.sh

clean:
	rm -rf node-rs/target runtime-py/.venv console/.venv tests/.venv deploy/local/.cookies

# Regenerate the vendored Python gRPC stubs from proto/ for every component (Rust stubs build via tonic-build/protox).
proto:
	./runtime-py/gen_proto.sh
	TMP=$$(mktemp -d) && mkdir -p $$TMP/ramen_console/proto/ramen_proto/ramen/v1 && cp proto/ramen/v1/*.proto $$TMP/ramen_console/proto/ramen_proto/ramen/v1/ && \
	  (cd console && uv run python -m grpc_tools.protoc -I $$TMP --python_out=src --grpc_python_out=src --pyi_out=src $$TMP/ramen_console/proto/ramen_proto/ramen/v1/*.proto)
	./tests/gen_proto.sh

# Local kind cluster for the v0.4.0 multi-zone proofs (CONTRACTS §12.3): two zones demo/a and demo/b, metrics-server
# for the HPA, the console chart with the GCP adapter's Kubernetes paths. See deploy/kind/README.md.
kind-up: build
	deploy/kind/up.sh

kind-test:
	deploy/kind/test.sh

kind-down:
	deploy/kind/down.sh
