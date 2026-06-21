"""Settings validation — SSRF guard on BOOKING_AGENT_URL and other config invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from booking_mcp.config import Settings

# --- BOOKING_AGENT_URL SSRF guard ---------------------------------------------


def test_booking_agent_url_allows_http():
    s = Settings(booking_agent_url="http://localhost:8000")
    assert s.booking_agent_url == "http://localhost:8000"


def test_booking_agent_url_allows_https():
    s = Settings(booking_agent_url="https://booking-agent.internal/api")
    assert s.booking_agent_url == "https://booking-agent.internal/api"


def test_booking_agent_url_allows_none():
    s = Settings(booking_agent_url=None)
    assert s.booking_agent_url is None


def test_booking_agent_url_blocks_file_scheme():
    with pytest.raises(ValidationError, match="http or https"):
        Settings(booking_agent_url="file:///etc/passwd")


def test_booking_agent_url_blocks_javascript_scheme():
    with pytest.raises(ValidationError, match="http or https"):
        Settings(booking_agent_url="javascript://evil")


def test_booking_agent_url_blocks_aws_metadata():
    with pytest.raises(ValidationError, match="metadata endpoint"):
        Settings(booking_agent_url="http://169.254.169.254/latest/meta-data/")


def test_booking_agent_url_blocks_gcp_metadata_hostname():
    with pytest.raises(ValidationError, match="metadata endpoint"):
        Settings(booking_agent_url="http://metadata.google.internal/v1/instance")


def test_booking_agent_url_blocks_generic_metadata_hostname():
    with pytest.raises(ValidationError, match="metadata endpoint"):
        Settings(booking_agent_url="http://metadata.internal/credentials")
