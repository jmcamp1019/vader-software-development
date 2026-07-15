"""Persistence layer for storing and querying trades."""

from .models import Trade


def save_trades(trades: list[Trade]) -> None:
    """Persist trades to storage. Stub."""
    raise NotImplementedError


def load_trades() -> list[Trade]:
    """Load previously stored trades. Stub."""
    raise NotImplementedError
