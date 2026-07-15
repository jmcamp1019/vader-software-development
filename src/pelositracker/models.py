"""Data models for filers and disclosed trades."""

from dataclasses import dataclass
from datetime import date


@dataclass
class Filer:
    name: str
    chamber: str  # "House" or "Senate"
    state: str


@dataclass
class Trade:
    filer: Filer
    ticker: str
    asset_description: str
    transaction_type: str  # "buy", "sell", "exchange"
    transaction_date: date
    disclosure_date: date
    amount_range: str
