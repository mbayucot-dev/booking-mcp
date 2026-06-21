# booking-mcp

A standalone [MCP](https://modelcontextprotocol.io) server (built on
[FastMCP](https://gofastmcp.com)) that exposes the booking datastore used by
[`booking-agent`](../booking-agent) to any MCP-compatible client. It is
decoupled from booking-agent and connects to the shared DB with its own
SQLAlchemy layer. booking-agent owns the
schema and migrations; a schema-contract test guards against drift.

## Features

**Resources** (read-only, URI-addressed)
| URI | Returns |
|---|---|
| `booking://staff` | active cleaners (skills + location) |
| `booking://staff/{staff_id}` | one staff member |
| `booking://schedule/{date}` | appointments on a date |
| `booking://clients/{email}` | client + contacts + saved preferences |

**Read tools** (`readOnly`, `idempotent`)
- `search_availability(service, date, time, latitude?, longitude?, radius_km?)`: staff who can do the job, are free at the slot, and are within range. This uses the same skill/free/geo filter as the booking engine.
- `find_next_available(service, date, time, days?, …)`: first day within the window with a free, qualified cleaner.
- `list_staff(skill?)`, `daily_schedule(date)`, `get_client(email)`.

**Write tools** (only when `READ_ONLY=false`; each asks for confirmation via MCP elicitation before writing)
- `create_booking(...)`: client + job + appointment, idempotent (deduped on a hash of all material fields).
- `cancel_booking(appointment_id)`: idempotent delete.
- `reschedule_booking(appointment_id, date, time)`: moves a slot and rejects staff conflicts.
- `add_customer_preference(email, note)`.
- `book_from_text(request)`: parses a free-text request using the client's LLM via MCP sampling, then confirms and books. Requires a sampling-capable client. Idempotent.

Writes go directly to the DB and bypass booking-agent's approval workflow. Use the workflow bridge below if you want human approval.

**Workflow bridge tools** (only when `BOOKING_AGENT_URL` is set; routes through booking-agent's human-approval workflow over HTTP)
- `book_via_workflow(message)`: start an approval run from a natural-language request and return `{run_id, status}`.
- `get_workflow_run(run_id)`: poll status and the final response.
- `decide_workflow_run(run_id, approve, by?, reason?)`: submit the approve/reject decision.

**Prompts**: `book_cleaning(...)`, `summarize_schedule(date)`.

All inputs are validated (real calendar dates/times, email format); all outputs
are typed (structured content).

## Quickstart

```bash
cp .env.example .env   # point DATABASE_URL at the shared Postgres
make install           # uv sync --dev
make dev-up            # start Postgres on :5433 (Docker)
make seed              # create_all + demo data (requires STANDALONE_MODE guard)
make server            # run in stdio mode
```

For the HTTP transport on the host: `make server-http` (binds `:8000`).

**Fully standalone (own DB + data)**. No booking-agent needed. One-shot the whole stack:

```bash
make stack-up   # docker compose up (db + seed + mcp on :8000)
```

`booking-mcp-seed` bootstraps the schema with `create_all` and populates demo staff, clients,
appointments, and preferences so the read tools return data immediately. `STANDALONE_MODE=true`
is required. The guard prevents accidental schema mutation against a shared DB. When sharing a
DB with booking-agent, skip the seed: booking-agent owns the canonical Alembic migrations.

## API / Usage

Any MCP client takes the standard `mcpServers` config (the same JSON an `mcp add` accepts).

**Local (stdio)**. The client launches the server as a subprocess. This is local and trusted, so no auth is required:

```json
{
  "mcpServers": {
    "booking": {
      "command": "/ABS/PATH/booking-mcp/.venv/bin/booking-mcp",
      "env": {
        "DATABASE_URL": "postgresql+psycopg://booking:booking@localhost:5432/booking",
        "READ_ONLY": "true"
      }
    }
  }
}
```

(`booking-mcp` is the console script installed into the venv.)

**Remote (HTTP)**. Connect over the streamable-HTTP transport with a Bearer key. Mint a key, then
pass the hash in `API_KEYS` (the server refuses to start write-enabled over HTTP without credentials):

```bash
# 1. Mint a key (prints plaintext once + the JSON record to add to API_KEYS)
#    Available scopes: read, write, workflow, pii (grant only what the client needs)
booking-mcp-mintkey --client claude-desktop --scopes read,write,pii

# 2. Start the server
API_KEYS='[{"hash":"<paste-hash>","client_id":"claude-desktop","scopes":["read","write","pii"]}]' \
  READ_ONLY=false booking-mcp
```

```json
{
  "mcpServers": {
    "booking": {
      "url": "http://your-host:8000/mcp",
      "headers": { "Authorization": "Bearer <plaintext-key>" }
    }
  }
}
```

A client with no or wrong key gets `401`. Scope enforcement is strict: a key without `read` cannot
see read tools; `write`/`workflow`/`pii` are additional gates on top. stdio needs no token
because it is local/trusted, so all surfaces are open.

> **Legacy**: `AUTH_TOKEN=<token>` still works as a single full-access fallback but is deprecated
> It grants read+write with no scope isolation. Migrate to `API_KEYS`.

## Development

Common `make` targets:

| Target | What it runs |
|---|---|
| `make install` | `uv sync --dev` |
| `make dev-up` / `dev-stop` / `dev-down` | Postgres container lifecycle |
| `make seed` | Schema + demo data (`STANDALONE_MODE=true`) |
| `make server` | stdio server on host |
| `make server-http` | HTTP server on host (`:8000`) |
| `make stack-up` / `stack-down` | Full containerised stack |
| `make mintkey ARGS="--client X --scopes read,pii"` | Mint an API key |
| `make db` | psql shell into the running container |
| `make test` | `pytest --cov=booking_mcp` |
| `make lint` | `ruff check src tests` |
| `make fmt` | `ruff format src tests` |
| `make audit` | `pip-audit` |
| `make check` | `lint` then `test` |

## Testing

```bash
make test   # pytest --cov=booking_mcp, requires 100% coverage to pass
```

- **In-memory client**: tools/resources are exercised through `fastmcp.Client` against the server object, with no subprocess.
- **Testcontainer Postgres**: the MCP's own `create_all` schema, truncated per test (real FK/types).
- **Schema-contract test** (`test_schema_contract.py`): when `../booking-agent/backend` is checked out, it applies booking-agent's **real Alembic migrations** to a fresh container and runs the MCP queries against them. This catches drift between this server's models and the owning service's schema. Skips when booking-agent isn't present.

## Configuration

Copy `.env.example` to `.env`. All settings are read from the environment (or `.env`).

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://booking:booking@localhost:5432/booking` | The same Postgres booking-agent uses; booking-agent owns the schema, this is a client. |
| `READ_ONLY` | `true` | Set to `false` to enable the write tools. Writes bypass booking-agent's human-approval workflow, so enable deliberately. |
| `STANDALONE_MODE` | `false` | **Must be `true`** to run `booking-mcp-seed` / `create_all()`. Guards against accidental schema mutation on a shared DB. Not needed when connecting to a DB already managed by booking-agent. |
| `API_KEYS` | _(empty)_ | Preferred HTTP auth. JSON array of `{hash, client_id, scopes}` records. Store hashes only, never plaintext. Mint records with `booking-mcp-mintkey`. All four scopes (`read`, `write`, `workflow`, `pii`) are enforced at the auth layer: a key sees only the surfaces its scopes explicitly cover. |
| `AUTH_TOKEN` | _(empty)_ | Deprecated: single static token granting full access (all scopes). Superseded by `API_KEYS`. Kept for backward compatibility. |
| `REDACT_PII` | `true` | Mask phone numbers (last-4 digits) and addresses (`[REDACTED]`) in client resources and `get_client`. Resources are pulled into model context, where PII can spread to prompts/logs/transcripts. Set `false` only for internal tooling backed by a scoped key. |
| `FORCE_WORKFLOW_FOR_SAMPLING` | `false` | Redirect `book_from_text` to `book_via_workflow` instead of writing directly. Recommended in production: sampled LLM output carries implicit trust/quota risks. |
| `BOOKING_AGENT_URL` | _(empty)_ | When set, the workflow-bridge tools are registered and POST to booking-agent so a booking goes through its full approval workflow. Decoupled: HTTP only, no import. |
| `BOOKING_AGENT_TIMEOUT` | `10.0` | HTTP timeout (seconds) for workflow-bridge calls to booking-agent. |
| `SAMPLE_TIMEOUT` | `30.0` | Cap on the client's LLM sampling call in `book_from_text` so a hung client can't pin a worker. |
| `DB_POOL_SIZE` | `20` | Connection pool size (sized for FastMCP's sync-tool threadpool). |
| `DB_MAX_OVERFLOW` | `20` | Pool overflow beyond `DB_POOL_SIZE`. |
| `DB_POOL_RECYCLE` | `3600` | Recycle connections after this many seconds. |
| `DB_POOL_TIMEOUT` | `10` | Seconds to wait for a pooled connection. |
| `DB_STATEMENT_TIMEOUT_MS` | `30000` | Per-query statement timeout (ms). |
| `LOG_LEVEL` | `INFO` | Logging level. |

## Notes
- **Schema ownership**: in standalone mode (`STANDALONE_MODE=true`), `booking-mcp-seed` bootstraps the schema with `create_all`. When sharing a DB with booking-agent, booking-agent owns the canonical Alembic migrations. Skip the seed entirely; the schema-contract test guards against model drift.
- No FastAPI/LangGraph. FastMCP brings its own (Starlette/uvicorn) HTTP stack for the HTTP transport.
- **MCP client features used**: *elicitation* (write confirmation), *sampling* (`book_from_text`). Both degrade gracefully. A client that does not support them just cannot call those tools.
- Per-resource content subscriptions and argument completions are not supported. Neither is first-class in this FastMCP version. Clients re-read `booking://schedule/{date}` for fresh data.

## License

MIT. See [LICENSE](LICENSE).
