# 🧩 booking-mcp

A **standalone [MCP](https://modelcontextprotocol.io) server** (built on
[FastMCP](https://gofastmcp.com)) that exposes the booking datastore — the same
PostgreSQL database the [`booking-agent`](../booking-agent) app uses — to any
MCP-compatible client. It is intentionally **decoupled**: it does not import
booking-agent, but connects to the shared DB with its own thin SQLAlchemy layer
as a read-first client. booking-agent **owns the schema + migrations**; a
schema-contract test guards against drift.

## Features

**Resources** (read-only, URI-addressed)
| URI | Returns |
|---|---|
| `booking://staff` | active cleaners (skills + location) |
| `booking://staff/{staff_id}` | one staff member |
| `booking://schedule/{date}` | appointments on a date |
| `booking://clients/{email}` | client + contacts + saved preferences |

**Tools — read** (`readOnly`, `idempotent`)
- `search_availability(service, date, time, latitude?, longitude?, radius_km?)` — staff who can do the job, are free at the slot, and within range (the same skill/free/geo filter the booking engine uses).
- `find_next_available(service, date, time, days?, …)` — first day within the window with a free, qualified cleaner.
- `list_staff(skill?)`, `daily_schedule(date)`, `get_client(email)`.

**Tools — write** (only when `READ_ONLY=false`; `destructive`. Each **asks the user to confirm via MCP elicitation** before writing.)
- `create_booking(...)` — client + job + appointment, idempotent (deduped on a hash of all material fields).
- `cancel_booking(appointment_id)` — idempotent delete.
- `reschedule_booking(appointment_id, date, time)` — moves a slot (rejects staff conflicts).
- `add_customer_preference(email, note)`.
- `book_from_text(request)` — parses a **free-text** request ("Book Jane a clean on June 20 at 10am, jane@…") using the **client's own LLM via MCP sampling**, then confirms + books. Requires a sampling-capable client. Idempotent.

*Writes go straight to the DB and bypass booking-agent's full LangGraph approval workflow — the elicitation confirmation is the safety gate here.*

**Tools — workflow bridge** (only when `BOOKING_AGENT_URL` is set; `openWorld`. The **safe** alternative to the direct writes — these go through booking-agent's full LangGraph human-approval gate over HTTP, no import.)
- `book_via_workflow(message)` — start a full approval run from a natural-language request → `{run_id, status}`.
- `get_workflow_run(run_id)` — poll status, the **approval card** (while paused), and the final response.
- `decide_workflow_run(run_id, approve, by?, reason?)` — the human-in-the-loop approve/reject decision.

**Prompts**: `book_cleaning(...)`, `summarize_schedule(date)`.

All inputs are validated (real calendar dates/times, email format); all outputs
are typed (structured content).

## Quickstart

```bash
cd booking-mcp
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # point DATABASE_URL at the shared Postgres
python -m booking_mcp.server  # stdio
# inspect interactively:
fastmcp dev src/booking_mcp/server.py
```

HTTP transport (remote): `build_server().run(transport="http", host="0.0.0.0", port=8000)` (the Docker default).

**Fully standalone (own DB + data)** — no booking-agent needed. One-shot the whole stack:

```bash
docker compose up        # Postgres + schema/seed + MCP server on :8000
```

…or against any empty Postgres, bootstrap the schema + demo data once, then run:

```bash
STANDALONE_MODE=true uv run booking-mcp-seed   # create_all + seed (requires STANDALONE_MODE)
uv run booking-mcp
```

`booking-mcp-seed` runs `create_all` plus demo staff/clients/appointments/preferences so the
read tools return data immediately. `STANDALONE_MODE=true` is required — the guard prevents
accidental schema mutation against a shared DB. When sharing a DB with booking-agent, skip
the seed: booking-agent owns the canonical Alembic migrations.

## API / Usage

Any MCP client takes the standard `mcpServers` config (the same JSON an `mcp add` accepts).

**Local (stdio)** — the client launches the server as a subprocess; local and trusted, so no auth:

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

**Remote (HTTP)** — connect over the streamable-HTTP transport with a Bearer key. Mint a key, then
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

A client with no/wrong key gets `401`. Scope enforcement is strict: a key without `read` cannot
see read tools; `write`/`workflow`/`pii` are additional gates on top. stdio needs no token
(local/trusted — all surfaces are open).

> **Legacy**: `AUTH_TOKEN=<token>` still works as a single full-access fallback but is deprecated
> — it grants read+write with no scope isolation. Migrate to `API_KEYS`.

## Testing

```bash
pytest --cov=booking_mcp        # in-memory FastMCP Client + Postgres testcontainer; 100% coverage
```

- **In-memory client**: tools/resources are exercised through `fastmcp.Client` against the server object — no subprocess.
- **Testcontainer Postgres**: the MCP's own `create_all` schema, truncated per test (real FK/types).
- **Schema-contract test** (`test_schema_contract.py`): when `../booking-agent/backend` is checked out, it applies booking-agent's **real Alembic migrations** to a fresh container and runs the MCP queries against them — catching any drift between this server's models and the owning service's schema. Skips when booking-agent isn't present.

## Configuration

Copy `.env.example` to `.env`. All settings are read from the environment (or `.env`).

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://booking:booking@localhost:5432/booking` | The same Postgres booking-agent uses; booking-agent owns the schema, this is a client. |
| `READ_ONLY` | `true` | Set to `false` to enable the write tools. Writes bypass booking-agent's human-approval workflow — enable deliberately. |
| `STANDALONE_MODE` | `false` | **Must be `true`** to run `booking-mcp-seed` / `create_all()`. Guards against accidental schema mutation on a shared DB. Not needed when connecting to a DB already managed by booking-agent. |
| `API_KEYS` | _(empty)_ | Preferred HTTP auth. JSON array of `{hash, client_id, scopes}` records — hashes only, never plaintext. Mint records with `booking-mcp-mintkey`. All four scopes (`read`, `write`, `workflow`, `pii`) are enforced at the auth layer: a key sees only the surfaces its scopes explicitly cover. |
| `AUTH_TOKEN` | _(empty)_ | Deprecated: single static token granting full access (all scopes). Superseded by `API_KEYS`. Kept for backward compatibility. |
| `REDACT_PII` | `true` | Mask phone numbers (last-4 digits) and addresses (`[REDACTED]`) in client resources and `get_client`. Resources are pulled into model context — PII spreads to prompts/logs/transcripts. Set `false` only for internal tooling backed by a scoped key. |
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
- **Schema ownership**: in standalone mode (`STANDALONE_MODE=true`), `booking-mcp-seed` bootstraps the schema with `create_all`. When sharing a DB with booking-agent, booking-agent owns the canonical Alembic migrations — skip the seed entirely, and the schema-contract test guards against model drift.
- No FastAPI/LangGraph — FastMCP brings its own (Starlette/uvicorn) HTTP stack for the HTTP transport.
- **MCP client features used**: *elicitation* (write confirmation), *sampling* (`book_from_text`). Both degrade gracefully — a client that doesn't support them just can't call those tools.
- **Not implemented (deliberately)**: per-resource *content subscriptions* (live `resources/updated` pushes) and *argument completions* aren't first-class in this FastMCP version (only `list_changed` notifications and no server-side `completion/complete` hook), so we don't hand-roll non-idiomatic plumbing for them. Clients re-read `booking://schedule/{date}` for fresh data.

## License

MIT — see [LICENSE](LICENSE).
