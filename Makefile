PYTHONPATH ?= src
UV_CACHE_DIR ?= .uv-cache
FERN_HOME ?= .fern-home
FERN_ENV = HOME=$(PWD)/$(FERN_HOME) FERN_NO_VERSION_REDIRECTION=true
KUBECONFIG ?= $(HOME)/.kube/incidentflow

.PHONY: openapi-generate openapi-validate docs-sync docs-contract-check docs-governance-check docs-publish-check fern-check fern-docs-dev fern-docs-generate fern-docs-publish docs-all \
        run-dev run-prod pf-dev pf-prod

openapi-generate:
	PYTHONPATH=$(PYTHONPATH) UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/generate_openapi.py

openapi-validate:
	PYTHONPATH=$(PYTHONPATH) UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/validate_openapi.py

docs-sync: openapi-generate
	python3 scripts/docs_governance.py sync

docs-contract-check:
	PYTHONPATH=$(PYTHONPATH) UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest -q \
		tests/test_tool_metadata.py tests/test_public_docs.py tests/test_docs_governance.py

docs-governance-check:
	python3 scripts/docs_governance.py validate

docs-publish-check: openapi-validate docs-contract-check
	python3 scripts/docs_governance.py publish

fern-check:
	cd fern && $(FERN_ENV) fern check

fern-docs-dev:
	cd fern && $(FERN_ENV) fern docs dev

fern-docs-generate:
	cd fern && $(FERN_ENV) fern generate --docs --preview

fern-docs-publish:
	cd fern && $(FERN_ENV) fern generate --docs

docs-all: openapi-generate openapi-validate docs-contract-check docs-governance-check fern-check

# Run MCP server locally against dev platform-api (localhost:8000)
run-dev:
	PYTHONPATH=$(PYTHONPATH) UV_CACHE_DIR=$(UV_CACHE_DIR) uv run --env-file .env.dev python -m incidentflow_mcp

# Run MCP server locally against prod platform-api (requires: make pf-prod first)
run-prod:
	PYTHONPATH=$(PYTHONPATH) UV_CACHE_DIR=$(UV_CACHE_DIR) uv run --env-file .env.prod python -m incidentflow_mcp

# Port-forward dev platform-api → localhost:8000
pf-dev:
	KUBECONFIG=$(KUBECONFIG) kubectl port-forward -n incidentflow-dev svc/incidentflow-platform-api 8000:8000

# Port-forward prod platform-api → localhost:8001
pf-prod:
	KUBECONFIG=$(KUBECONFIG) kubectl port-forward -n incidentflow-prod svc/incidentflow-platform-api 8001:8000
