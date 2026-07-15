"""Parsing of STOCK Act dollar-range strings.

Disclosures report ranges like "$1,001 - $15,000" or open-ended
"$50,000,000 +". Ranges are NEVER collapsed to a point estimate:
we store integer-cent bounds, with an open upper bound as None.
"""
from __future__ import annotations

import re

_MONEY_PATTERN = re.compile(r"\$\s*([\d,]+)")


def parse_amount_range(raw: str | None) -> tuple[int, int | None]:
    """Parse a disclosure amount string into (min_cents, max_cents_or_None)."""
    if raw is None or not raw.strip():
        raise ValueError("empty amount string")
    matches = _MONEY_PATTERN.findall(raw)
    if not matches:
        raise ValueError(f"unparseable amount string: {raw!r}")
    values = [int(m.replace(",", "")) for m in matches]
    min_cents = values[0] * 100
    max_cents: int | None = values[1] * 100 if len(values) > 1 else None
    if max_cents is not None and max_cents < min_cents:
        raise ValueError(f"inverted amount range: {raw!r}")
    return min_cents, max_cents
