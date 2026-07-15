"""Fetch disclosure JSON from remote endpoints or local fixture files."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

from . import config


def fetch_json(url: str, timeout: int | None = None) -> list[dict[str, Any]]:
    """Fetch and decode a JSON array from a URL (stdlib only)."""
    request = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout or config.HTTP_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected a JSON array from {url}, got {type(payload).__name__}")
    return payload


def load_fixture(path: str | Path) -> list[dict[str, Any]]:
    """Load a local JSON fixture file with the same shape as the live feeds."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Fixture {path} must contain a JSON array")
    return data
