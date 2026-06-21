"""Per-client API-key auth for the HTTP transport.

Keys are high-entropy (``bmcp_`` + 256-bit url-safe token), so we store only a
SHA-256 hash at rest and compare in constant time — no slow KDF needed (a KDF
defends low-entropy passwords against brute force; these tokens aren't guessable).
Each key carries its own client_id and scopes (read / write), and an optional
expiry, which gives per-client least privilege and rotation (multiple keys live
at once). stdio is local/trusted and stays unauthenticated.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import TYPE_CHECKING

from fastmcp.server.auth import AccessToken, TokenVerifier

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger("booking_mcp.auth")

# Scope names. Use least-privilege when minting keys.
READ = "read"        # read-only tools + non-PII resources
WRITE = "write"      # direct DB write tools (bypass approval workflow)
WORKFLOW = "workflow"  # booking-agent workflow tools (approve/reject)
PII = "pii"          # get_client tool and booking://clients/ resources

_REQUIRED_FIELDS = ("hash", "client_id", "scopes")


def hash_key(key: str) -> str:
    """SHA-256 hex digest of a plaintext key — what we store and compare against."""
    return hashlib.sha256(key.encode()).hexdigest()


def mint_key() -> tuple[str, str]:
    """Generate a fresh API key: returns (plaintext, hash). Show the plaintext once."""
    plaintext = f"bmcp_{secrets.token_urlsafe(32)}"
    return plaintext, hash_key(plaintext)


def load_api_keys(settings: Settings) -> list[dict]:
    """Build the active key records from settings.

    Primary source is ``API_KEYS`` — a JSON array of records, each with ``hash``,
    ``client_id``, ``scopes`` (and optional ``expires_at`` epoch seconds). A bare
    ``AUTH_TOKEN`` is still honoured as the deprecated single-key fallback: it maps
    to one full-access (read+write) record. Raises ValueError on malformed JSON or
    a record missing a required field.
    """
    records: list[dict] = []
    if settings.api_keys:
        try:
            parsed = json.loads(settings.api_keys)
        except json.JSONDecodeError as e:
            raise ValueError(f"API_KEYS is not valid JSON: {e}") from e
        if not isinstance(parsed, list):
            raise ValueError("API_KEYS must be a JSON array of key records.")
        for i, rec in enumerate(parsed):
            if not isinstance(rec, dict):
                raise ValueError(f"API_KEYS[{i}] must be an object, got {type(rec).__name__}.")
            missing = [f for f in _REQUIRED_FIELDS if f not in rec]
            if missing:
                raise ValueError(f"API_KEYS[{i}] is missing required field(s): {missing}.")
            records.append(rec)

    if settings.auth_token:
        log.warning(
            "AUTH_TOKEN is deprecated: it grants a single full-access (read+write) key. "
            "Migrate to API_KEYS with per-client hashed keys and least-privilege scopes."
        )
        records.append(
            {
                "hash": hash_key(settings.auth_token),
                "client_id": "booking-mcp",
                # Full access — matches the pre-scope behaviour of the single shared token.
                "scopes": [READ, WRITE, WORKFLOW, PII],
            }
        )
    return records


class HashedApiKeyVerifier(TokenVerifier):
    """Verify a bearer token against SHA-256 hashes of the configured API keys.

    Mirrors FastMCP's StaticTokenVerifier, but never holds plaintext keys: the
    presented token is hashed and matched against stored hashes in constant time.
    Honours per-record ``expires_at`` (epoch seconds) so a key can be retired
    without removing it. Returns an AccessToken carrying the record's client_id
    and scopes, or None when no key matches / the matched key has expired.
    """

    def __init__(self, records: list[dict], required_scopes: list[str] | None = None):
        super().__init__(required_scopes=required_scopes)
        self.records = records

    async def verify_token(self, token: str) -> AccessToken | None:
        presented = hash_key(token)
        for rec in self.records:
            if not hmac.compare_digest(presented, rec["hash"]):
                continue
            expires_at = rec.get("expires_at")
            if expires_at is not None and expires_at < time.time():
                return None
            return AccessToken(
                token=token,
                client_id=rec["client_id"],
                scopes=rec.get("scopes", []),
                expires_at=expires_at,
                claims=rec,
            )
        return None
