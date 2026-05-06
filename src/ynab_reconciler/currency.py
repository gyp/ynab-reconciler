"""Currency parsing and conversion via Frankfurter API."""
from __future__ import annotations

import re

import requests

FRANKFURTER_BASE = "https://api.frankfurter.dev/v2"


def parse_statement_input(raw: str) -> tuple[float, str | None]:
    """Parse user input into (amount, iso_code_or_None).

    "1000 EUR" → (1000.0, "EUR")
    "1200.50"  → (1200.50, None)
    Raises ValueError on unparseable input.
    """
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s+([A-Z]{3})", raw.strip())
    if m:
        return float(m.group(1)), m.group(2)
    return float(raw), None


def convert_currency(amount: float, from_iso: str, to_iso: str) -> float:
    """Return amount converted from from_iso to to_iso using Frankfurter spot rate."""
    resp = requests.get(f"{FRANKFURTER_BASE}/rate/{from_iso}/{to_iso}", timeout=10)
    if not resp.ok:
        raise ValueError(f"Currency conversion failed ({resp.status_code}): {resp.text}")
    rate = resp.json()["rate"]
    return amount * rate
