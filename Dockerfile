FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Drop privileges: run as an unprivileged user, not root.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

ENV READ_ONLY=true
# DATABASE_URL must point at the shared Postgres (booking-agent owns the schema).

# Containers serve over HTTP (stdio servers are launched by the MCP client, not
# run standalone). Override for stdio if embedding.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/_health').status==200 else 1)"]
CMD ["python", "-c", "from booking_mcp.server import build_server; build_server(transport='http').run(transport='http', host='0.0.0.0', port=8000)"]
