"""CLI to mint a new API key.

    booking-mcp-mintkey --client acme --scopes read,write [--expires-days 90]

Prints the plaintext key ONCE (give it to the client, then forget it) plus the
JSON record to paste into API_KEYS. Only the hash is stored server-side.
"""

from __future__ import annotations

import argparse
import json
import time

from .auth import PII, READ, WORKFLOW, WRITE, mint_key

_VALID_SCOPES = {READ, WRITE, WORKFLOW, PII}
_VALID_SCOPES_STR = "read, write, workflow, pii"


def _parse_scopes(raw: str) -> list[str]:
    scopes = [s.strip() for s in raw.split(",") if s.strip()]
    if not scopes:
        raise SystemExit(f"ERROR: --scopes must list at least one of: {_VALID_SCOPES_STR}")
    bad = sorted(set(scopes) - _VALID_SCOPES)
    if bad:
        raise SystemExit(f"ERROR: unknown scope(s) {bad}; valid scopes are: {_VALID_SCOPES_STR}")
    return scopes


def main() -> None:
    parser = argparse.ArgumentParser(prog="booking-mcp-mintkey", description=__doc__)
    parser.add_argument("--client", required=True, help="Client id this key identifies")
    parser.add_argument(
        "--scopes", required=True, help="Comma-separated scopes, e.g. read or read,write"
    )
    parser.add_argument(
        "--expires-days",
        type=int,
        default=None,
        help="Optional lifetime in days; omit for a non-expiring key",
    )
    args = parser.parse_args()

    scopes = _parse_scopes(args.scopes)
    plaintext, key_hash = mint_key()
    record: dict = {"hash": key_hash, "client_id": args.client, "scopes": scopes}
    if args.expires_days is not None:
        record["expires_at"] = int(time.time()) + args.expires_days * 86400

    print(f"Plaintext key (shown ONCE — store it now, it is not recoverable):\n{plaintext}\n")
    print("Record to add to the API_KEYS JSON array (hash only — never store the plaintext):")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
