.PHONY: install dev-up dev-stop dev-down seed server server-http test lint fmt audit db mintkey stack-up stack-down

# Local dev: Postgres in Docker, server + tests on the host.
# Full containerised stack: `make stack-up` (db + seed + mcp all in containers).
COMPOSE := docker compose
UV      := uv run

# ── Setup ────────────────────────────────────────────────────────────────────

install:
	uv sync --dev

# ── Local dev infra ───────────────────────────────────────────────────────────

dev-up:
	$(COMPOSE) up -d db
	@echo "Waiting for Postgres..."
	@$(COMPOSE) exec db sh -c 'until pg_isready -U booking -q; do sleep 1; done'
	@echo "Postgres ready on :5433"

dev-stop:
	$(COMPOSE) stop db

dev-down:
	$(COMPOSE) down -v

# ── Data ─────────────────────────────────────────────────────────────────────

seed:
	STANDALONE_MODE=true $(UV) booking-mcp-seed

# ── Run (host) ───────────────────────────────────────────────────────────────

server:
	$(UV) booking-mcp

server-http:
	$(UV) python -c "from booking_mcp.server import build_server; build_server(transport='http').run(transport='http', host='0.0.0.0', port=8000)"

# ── Full containerised stack ─────────────────────────────────────────────────

stack-up:
	$(COMPOSE) up

stack-down:
	$(COMPOSE) down -v

# ── Auth ─────────────────────────────────────────────────────────────────────

# Usage: make mintkey ARGS="--client acme --scopes read,pii [--expires-days 90]"
mintkey:
	@$(UV) booking-mcp-mintkey $(ARGS)

# ── Database shell ────────────────────────────────────────────────────────────

db:
	$(COMPOSE) exec db psql -U booking -d booking

# ── Quality ──────────────────────────────────────────────────────────────────

test:
	$(UV) pytest --cov=booking_mcp

lint:
	$(UV) ruff check src tests

fmt:
	$(UV) ruff format src tests

audit:
	$(UV) pip-audit

check: lint test
