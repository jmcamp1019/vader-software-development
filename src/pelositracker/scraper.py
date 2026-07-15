"""Fetches and parses periodic transaction report (PTR) disclosure filings."""

from .models import Trade


def fetch_filings() -> list[Trade]:
    """Fetch raw disclosure filings from the source. Stub."""
    raise NotImplementedError


def parse_filing(raw: bytes) -> list[Trade]:
    """Parse a single filing document into Trade records. Stub."""
    raise NotImplementedError
