"""Settings for the standalone MCP server (env / .env)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # When set, HTTP clients must send `Authorization: Bearer <token>`. Ignored for stdio.
    auth_token: str | None = None

    # When set, the workflow-bridge tools are registered and POST to booking-agent so a
    # booking goes through its full approval workflow. Decoupled: HTTP only, no import.
    booking_agent_url: str | None = None
    booking_agent_timeout: float = 10.0


def get_settings() -> Settings:
    return Settings()
