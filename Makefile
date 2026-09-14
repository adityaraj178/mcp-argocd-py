UV ?= uv
PYTHON := .venv/bin/python

# Show this help by default: list every target that has a `## ` doc comment.
.DEFAULT_GOAL := help
.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# Local run configuration. Override on the command line, e.g.
#   make run PORT=4000
# By default the server starts with no credentials configured; supply tokens via
# environment variables (ARGOCD_API_TOKEN / ARGOCD_BASE_URL) or a registry
# (ARGOCD_TOKEN_REGISTRY_PATH). See "Running locally" in the README.
PORT ?= 3000

# Sentinel target: re-run install only when the lockfile or manifest changes.
.venv: pyproject.toml uv.lock
	$(UV) sync --all-groups
	@touch .venv

.PHONY: install
install: .venv ## Install dependencies into .venv

.PHONY: lint
lint: .venv ## Run the linter, formatter check, and type checker
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests
	$(UV) run mypy

.PHONY: fmt
fmt: .venv ## Format the code and apply safe lint fixes
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests

.PHONY: test
test: .venv ## Run the test suite
	$(UV) run pytest

.PHONY: build
build: .venv ## Build the sdist and wheel into dist/
	$(UV) build

# Both targets run the server with whatever credentials are already in the
# environment. To configure them, export the relevant env var on the command
# line, e.g. `make run ARGOCD_BASE_URL=... ARGOCD_API_TOKEN=...` or
# `make run ARGOCD_TOKEN_REGISTRY_PATH=/path/to/tokens.json`.
.PHONY: run
run: .venv ## Run the server over HTTP
	$(UV) run argocd-mcp http --port $(PORT)

.PHONY: dev
dev: .venv ## Run the server over HTTP, restarting when the source changes
	$(UV) run --with watchfiles watchfiles --filter python \
		"$(PYTHON) -m argocd_mcp http --port $(PORT)" src

.PHONY: docker
docker: ## Build the container image
	docker build -t argocd-mcp .
