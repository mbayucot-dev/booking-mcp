"""Settings for the standalone MCP server (env / .env)."""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Cloud instance-metadata endpoints that must never be reachable via BOOKING_AGENT_URL.
# An SSRF hit here hands an attacker IAM credentials from the cloud provider.
_METADATA_HOSTS: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS/GCP/Azure IMDS (link-local)
        "metadata.google.internal",
        "metadata.internal",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # The same Postgres booking-agent uses; booking-agent owns the schema, this is a client.
    database_url: str = "postgresql+psycopg://booking:booking@localhost:5432/booking"

    # Write tools bypass booking-agent's human-approval workflow, so they're opt-in.
    read_only: bool = True

    # Sized for FastMCP's sync-tool threadpool under concurrent clients.
    db_pool_size: int = 20
    db_max_overflow: int = 20
    db_pool_recycle: int = 3600
    db_pool_timeout: int = 10
    db_statement_timeout_ms: int = 30000

    # Cap on the client's LLM sampling call in book_from_text so a hung client can't pin a worker.
    sample_timeout: float = 30.0

    log_level: str = "INFO"

    # Must be explicitly true to allow create_all() / seed to run.
    # Prevents accidental schema mutation against the shared booking-agent DB.
    standalone_mode: bool = False

    # Mask phone/address in client resources and tools before returning to MCP clients.
    # Resources are pulled into model context, so PII spreads to prompts/logs/transcripts.
    # Set false only for internal tooling (e.g. admin API backed by a scoped token).
    redact_pii: bool = True

    # When set, HTTP clients must send `Authorization: Bearer <token>`. Ignored for stdio.
    auth_token: str | None = None

    # When set, the workflow-bridge tools are registered and POST to booking-agent so a
    # booking goes through its full approval workflow. Decoupled: HTTP only, no import.
    booking_agent_url: str | None = None
    booking_agent_timeout: float = 10.0

    # When true, book_from_text refuses to do a direct DB write and redirects callers to
    # book_via_workflow. Recommended in production: sampled output has implicit trust risks
    # (quota abuse, hidden prompt manipulation) and should go through the approval workflow.
    force_workflow_for_sampling: bool = False

    @field_validator("booking_agent_url")
    @classmethod
    def _check_booking_agent_url(cls, v: str | None) -> str | None:
        """Block SSRF attack vectors via a misconfigured BOOKING_AGENT_URL.

        The most dangerous scenario is a metadata endpoint URL that would let
        booking-mcp act as a confused deputy and leak cloud IAM credentials.
        Non-http/https schemes (file://, javascript://) are also blocked.
        Private IP ranges are *not* blocked here — they are legitimate in
        internal/dev deployments. Only metadata endpoints are hard-blocked.
        """
        if v is None:
            return None
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"BOOKING_AGENT_URL scheme must be http or https; got {parsed.scheme!r}. "
                "file://, javascript://, and other non-HTTP schemes are blocked (SSRF risk)."
            )
        host = (parsed.hostname or "").lower()
        if host in _METADATA_HOSTS:
            raise ValueError(
                f"BOOKING_AGENT_URL host {host!r} is a cloud metadata endpoint. "
                "Requests to instance-metadata addresses are blocked to prevent "
                "IAM credential theft via SSRF."
            )
        return v


def get_settings() -> Settings:
    return Settings()
