FROM python:3.11-slim

# Install uv for reproducible, lockfile-based installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src

# Install only runtime deps from the lockfile (no dev extras).
# --frozen: fail if uv.lock is out of sync with pyproject.toml.
# --no-dev: skip dev extras (pytest, ruff, pip-audit, etc.).
RUN uv sync --frozen --no-dev

# Drop privileges: run as an unprivileged user, not root.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

ENV READ_ONLY=true
# DATABASE_URL must point at the shared Postgres (booking-agent owns the schema).

# Containers serve over HTTP (stdio servers are launched by the MCP client, not
# run standalone). Override for stdio if embedding.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/_health').status==200 else 1)"]
CMD ["uv", "run", "python", "-c", "from booking_mcp.server import build_server; build_server(transport='http').run(transport='http', host='0.0.0.0', port=8000)"]
