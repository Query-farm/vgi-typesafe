# Common tasks. `make help` lists them. Requires uv (https://docs.astral.sh/uv/).

HAYBARN_VERSION ?= 2.0.1rc3
WORKER          := $(CURDIR)/.venv/bin/vgi-typesafe
TOOLS           := $(CURDIR)/.tools
HAYBARN_UNITTEST := $(TOOLS)/bin/haybarn-unittest
TRANSPORT       ?= stdio

.DEFAULT_GOAL := help
.PHONY: help build tools lint test-unit test-sql test-sql-all test lint-explain check clean

help: ## List the targets
	@awk 'BEGIN{FS=":.*## "} /^[a-zA-Z_-]+:.*## /{printf "  %-14s %s\n",$$1,$$2}' $(MAKEFILE_LIST)

build: ## Install the worker and dev tools into .venv
	uv sync --locked --all-extras

tools: ## Install the pinned SQL test runner into .tools
	@test -x $(HAYBARN_UNITTEST) || { uv venv -q $(TOOLS) && uv pip install -q -p $(TOOLS) haybarn-unittest==$(HAYBARN_VERSION); }

lint: build ## Static checks: ruff, mypy (pydoclint runs inside pytest)
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy vgi_typesafe/

test-unit: build ## Offline unit tests (pytest; no key, no network)
	uv run pytest -q

test-sql: build tools ## SQL tests through DuckDB against the mock: make test-sql TRANSPORT=stdio|launch|http
	WORKER=$(WORKER) HAYBARN_UNITTEST=$(HAYBARN_UNITTEST) TRANSPORT=$(TRANSPORT) PYTHON="uv run --no-project python" ci/run-integration.sh

test-sql-all: ## SQL tests over every transport
	$(MAKE) test-sql TRANSPORT=stdio
	$(MAKE) test-sql TRANSPORT=launch
	$(MAKE) test-sql TRANSPORT=http

test: test-unit test-sql-all ## Unit + SQL tests

lint-explain: build ## Metadata quality (vgi-lint, behavioural tier) against the bundled mock
	@uv run vgi-typesafe-mock --port 8787 --api-key ci-key & mock_pid=$$!; \
	  trap 'kill $$mock_pid 2>/dev/null || true' EXIT; \
	  for _ in $$(seq 1 50); do curl -s -o /dev/null http://127.0.0.1:8787/v1/models && break; sleep 0.2; done; \
	  TYPESAFE_API_KEY=ci-key TYPESAFE_BASE_URL=http://127.0.0.1:8787 \
	    uvx --from vgi-lint-check vgi-lint lint --execute --audit-waivers --no-check-links

check: lint test-unit test-sql-all lint-explain ## Everything CI runs. Done = this passes.

clean: ## Remove build outputs and tool caches
	rm -rf .venv .tools dist build .mypy_cache .ruff_cache .pytest_cache
