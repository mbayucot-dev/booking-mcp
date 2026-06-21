"""Per-client API-key auth: hashing, key minting, key loading, the verifier, and the CLI."""

from __future__ import annotations

import json
import time

import pytest

from booking_mcp import mint_key
from booking_mcp.auth import (
    PII,
    READ,
    WORKFLOW,
    WRITE,
    HashedApiKeyVerifier,
    hash_key,
    load_api_keys,
)
from booking_mcp.auth import (
    mint_key as mint_key_fn,
)
from booking_mcp.config import Settings

# --- hash_key / mint_key ------------------------------------------------------


def test_hash_key_is_deterministic_sha256_hex():
    h = hash_key("bmcp_example")
    assert h == hash_key("bmcp_example")
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_mint_key_prefix_entropy_and_hash_round_trip():
    plaintext, key_hash = mint_key_fn()
    assert plaintext.startswith("bmcp_")
    # token_urlsafe(32) → ~43 chars of base64url after the prefix.
    assert len(plaintext) > 40
    assert hash_key(plaintext) == key_hash
    # Two mints differ (high entropy).
    assert mint_key_fn()[0] != mint_key_fn()[0]


# --- load_api_keys ------------------------------------------------------------


def test_load_api_keys_parses_valid_json():
    rec = {"hash": hash_key("k"), "client_id": "acme", "scopes": [READ, WRITE]}
    keys = load_api_keys(Settings(api_keys=json.dumps([rec])))
    assert keys == [rec]


def test_load_api_keys_auth_token_fallback_grants_all_scopes(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="booking_mcp.auth"):
        keys = load_api_keys(Settings(auth_token="legacy-secret", api_keys=None))
    # AUTH_TOKEN is "full access" by design — it predates per-scope keys. Granting all four
    # scopes preserves backward compatibility (nothing is hidden from a legacy token).
    assert keys == [
        {
            "hash": hash_key("legacy-secret"),
            "client_id": "booking-mcp",
            "scopes": [READ, WRITE, WORKFLOW, PII],
        }
    ]
    assert any("AUTH_TOKEN is deprecated" in r.message for r in caplog.records)


def test_load_api_keys_combines_api_keys_and_auth_token():
    rec = {"hash": hash_key("k"), "client_id": "acme", "scopes": [READ]}
    keys = load_api_keys(Settings(api_keys=json.dumps([rec]), auth_token="legacy"))
    assert len(keys) == 2
    assert keys[0]["client_id"] == "acme"
    assert keys[1]["client_id"] == "booking-mcp"


def test_load_api_keys_malformed_json_raises():
    with pytest.raises(ValueError, match="not valid JSON"):
        load_api_keys(Settings(api_keys="{not json"))


def test_load_api_keys_non_array_raises():
    with pytest.raises(ValueError, match="must be a JSON array"):
        load_api_keys(Settings(api_keys=json.dumps({"hash": "x"})))


def test_load_api_keys_non_object_record_raises():
    with pytest.raises(ValueError, match="must be an object"):
        load_api_keys(Settings(api_keys=json.dumps(["not-an-object"])))


def test_load_api_keys_missing_field_raises():
    with pytest.raises(ValueError, match="missing required field"):
        load_api_keys(Settings(api_keys=json.dumps([{"hash": "x", "client_id": "acme"}])))


def test_load_api_keys_empty_when_nothing_configured():
    assert load_api_keys(Settings(api_keys=None, auth_token=None)) == []


# --- HashedApiKeyVerifier -----------------------------------------------------


async def test_verifier_accepts_valid_token_with_client_and_scopes():
    plaintext, key_hash = mint_key_fn()
    verifier = HashedApiKeyVerifier(
        [{"hash": key_hash, "client_id": "acme", "scopes": [READ, WRITE]}]
    )
    token = await verifier.verify_token(plaintext)
    assert token is not None
    assert token.client_id == "acme"
    assert set(token.scopes) == {READ, WRITE}


async def test_verifier_rejects_unknown_token():
    plaintext, key_hash = mint_key_fn()
    verifier = HashedApiKeyVerifier([{"hash": key_hash, "client_id": "acme", "scopes": [READ]}])
    assert await verifier.verify_token("bmcp_not-a-real-key") is None


async def test_verifier_rejects_expired_token():
    plaintext, key_hash = mint_key_fn()
    verifier = HashedApiKeyVerifier(
        [
            {
                "hash": key_hash,
                "client_id": "acme",
                "scopes": [READ],
                "expires_at": int(time.time()) - 10,
            }
        ]
    )
    assert await verifier.verify_token(plaintext) is None


async def test_verifier_accepts_unexpired_token():
    plaintext, key_hash = mint_key_fn()
    verifier = HashedApiKeyVerifier(
        [
            {
                "hash": key_hash,
                "client_id": "acme",
                "scopes": [READ],
                "expires_at": int(time.time()) + 3600,
            }
        ]
    )
    token = await verifier.verify_token(plaintext)
    assert token is not None and token.expires_at is not None


# --- mint_key CLI -------------------------------------------------------------


def _run_cli(monkeypatch, argv: list[str]):
    monkeypatch.setattr("sys.argv", ["booking-mcp-mintkey", *argv])
    mint_key.main()


def test_cli_prints_plaintext_and_record(monkeypatch, capsys):
    _run_cli(monkeypatch, ["--client", "acme", "--scopes", "read,write"])
    out = capsys.readouterr().out
    assert "bmcp_" in out
    # The printed record parses and round-trips: its hash matches the printed plaintext.
    rec = json.loads(out[out.index("{") :])
    assert rec["client_id"] == "acme"
    assert rec["scopes"] == [READ, WRITE]
    assert "expires_at" not in rec
    plaintext = next(line for line in out.splitlines() if line.startswith("bmcp_"))
    assert rec["hash"] == hash_key(plaintext)


def test_cli_with_expiry_emits_expires_at(monkeypatch, capsys):
    before = int(time.time())
    _run_cli(monkeypatch, ["--client", "acme", "--scopes", "read", "--expires-days", "30"])
    out = capsys.readouterr().out
    rec = json.loads(out[out.index("{") :])
    assert rec["scopes"] == [READ]
    assert before + 30 * 86400 <= rec["expires_at"] <= int(time.time()) + 30 * 86400 + 5


def test_cli_rejects_unknown_scope(monkeypatch):
    with pytest.raises(SystemExit, match="unknown scope"):
        _run_cli(monkeypatch, ["--client", "acme", "--scopes", "read,admin"])


def test_cli_rejects_empty_scopes(monkeypatch):
    with pytest.raises(SystemExit, match="at least one"):
        _run_cli(monkeypatch, ["--client", "acme", "--scopes", " , "])


# --- scope constants ----------------------------------------------------------


def test_scope_constants_exist_and_are_distinct():
    assert READ == "read"
    assert WRITE == "write"
    assert WORKFLOW == "workflow"
    assert PII == "pii"
    assert len({READ, WRITE, WORKFLOW, PII}) == 4


def test_cli_accepts_workflow_scope(monkeypatch, capsys):
    _run_cli(monkeypatch, ["--client", "acme", "--scopes", "workflow"])
    out = capsys.readouterr().out
    rec = json.loads(out[out.index("{") :])
    assert rec["scopes"] == [WORKFLOW]


def test_cli_accepts_pii_scope(monkeypatch, capsys):
    _run_cli(monkeypatch, ["--client", "acme", "--scopes", "pii"])
    out = capsys.readouterr().out
    rec = json.loads(out[out.index("{") :])
    assert rec["scopes"] == [PII]


def test_cli_accepts_all_four_scopes(monkeypatch, capsys):
    _run_cli(monkeypatch, ["--client", "ops", "--scopes", "read,write,workflow,pii"])
    out = capsys.readouterr().out
    rec = json.loads(out[out.index("{") :])
    assert set(rec["scopes"]) == {READ, WRITE, WORKFLOW, PII}


def test_cli_rejects_unknown_scope_still_rejected(monkeypatch):
    with pytest.raises(SystemExit, match="unknown scope"):
        _run_cli(monkeypatch, ["--client", "acme", "--scopes", "read,admin"])
