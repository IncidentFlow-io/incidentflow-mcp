PYTHONPATH ?= src
UV_CACHE_DIR ?= .uv-cache
FERN_HOME ?= .fern-home
FERN_ENV = HOME=$(PWD)/$(FERN_HOME) FERN_NO_VERSION_REDIRECTION=true

.PHONY: openapi-generate openapi-validate fern-check fern-docs-dev fern-docs-generate fern-docs-publish docs-all

openapi-generate:
	PYTHONPATH=$(PYTHONPATH) UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/generate_openapi.py

openapi-validate:
	PYTHONPATH=$(PYTHONPATH) UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/validate_openapi.py

fern-check:
	cd fern && $(FERN_ENV) fern check

fern-docs-dev:
	cd fern && $(FERN_ENV) fern docs dev

fern-docs-generate:
	cd fern && $(FERN_ENV) fern generate --docs --preview

fern-docs-publish:
	cd fern && $(FERN_ENV) fern generate --docs

docs-all: openapi-generate openapi-validate fern-check
