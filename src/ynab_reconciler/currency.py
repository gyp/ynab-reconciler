"""Currency parsing and conversion via Frankfurter API."""
from __future__ import annotations

import re

import requests

FRANKFURTER_BASE = "https://api.frankfurter.dev/v2"


def parse_statement_input(raw: str) -> tuple[float, str | None]:
    """Parse user input into (amount, iso_code_or_None).

    Accepts many copy-pasted formats: "1,000,000.00", "1 000 000,00",
    "1.234,56 EUR", "-500 USD", etc. Raises ValueError on unparseable input.
    """
    s = raw.strip()
    iso: str | None = None
    m = re.fullmatch(r"(.+?)\s+([A-Z]{3})", s)
    if m:
        s, iso = m.group(1).strip(), m.group(2)

    return _parse_number(s), iso


def _parse_number(s: str) -> float:
    sign = ""
    if s[:1] in "+-":
        sign, s = s[0], s[1:]

    s = s.replace(" ", "").replace(" ", "")
    if not s or not re.fullmatch(r"[\d.,]+", s):
        raise ValueError(f"Cannot parse number: {s!r}")

    has_comma = "," in s
    has_dot = "." in s
    if has_comma and has_dot:
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        s = s.replace(thousands, "").replace(decimal, ".")
    elif has_comma or has_dot:
        sep = "," if has_comma else "."
        count = s.count(sep)
        tail = s.rsplit(sep, 1)[1]
        is_thousands = count > 1 or len(tail) == 3
        if is_thousands:
            s = s.replace(sep, "")
        elif sep == ",":
            s = s.replace(",", ".")

    return float(sign + s)


def convert_currency(amount: float, from_iso: str, to_iso: str) -> float:
    """Return amount converted from from_iso to to_iso using Frankfurter spot rate."""
    resp = requests.get(f"{FRANKFURTER_BASE}/rate/{from_iso}/{to_iso}", timeout=10)
    if not resp.ok:
        raise ValueError(f"Currency conversion failed ({resp.status_code}): {resp.text}")
    rate = resp.json()["rate"]
    return amount * rate
