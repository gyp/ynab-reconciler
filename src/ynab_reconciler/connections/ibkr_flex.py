"""Interactive Brokers Flex Web Service client (read-only balance fetch).

The Flex Web Service is a plain-HTTPS, read-only report API: a Flex token can
only retrieve a pre-defined report, never place orders or move funds. Fetching a
report is a two-step request:

  1. SendRequest(token, query_id)  -> a ReferenceCode + a GetStatement Url
  2. GetStatement(token, ref_code) -> the report XML (may not be ready instantly)

Data is **end-of-day**, not real-time — fine for budgeting, but surfaced in the
CLI help so the user isn't surprised. We respect the service's pacing limit
(1 req/sec, 10/min) with a short sleep between calls and on retries.

No long-running process, no TWS/IB Gateway, no credentials — only the token,
which is passed in by the caller and never logged.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from typing import Optional

import requests

from .errors import IbkrAuthError, IbkrFlexError, IbkrReportNotReady
from .models import CASH, NET_LIQUIDATION, FetchedBalance

SEND_REQUEST_URL = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
)
API_VERSION = "3"

# Flex error code returned while a statement is still being generated.
_IN_PROGRESS_CODE = "1019"
# Pacing / retry tuning (kept small; the report is usually ready in a few s).
_PACING_SECONDS = 1.0
_MAX_STATEMENT_ATTEMPTS = 4
_RETRY_BACKOFF_SECONDS = 2.0


class IbkrFlexClient:
    """Fetches account balances from an IBKR Activity Flex Query."""

    def __init__(
        self,
        token: str,
        *,
        session: Optional[requests.Session] = None,
        timeout: int = 15,
    ) -> None:
        self._token = token
        self._session = session or requests.Session()
        self._timeout = timeout

    def fetch_balances(self, query_id: str) -> list[FetchedBalance]:
        """Run the two-step Flex flow and return the balances in the report.

        Raises IbkrAuthError on a rejected token/query, IbkrReportNotReady if the
        statement never finished generating, IbkrFlexError for other Flex errors,
        and requests.RequestException on network failures.
        """
        reference_code, statement_url = self._send_request(query_id)
        time.sleep(_PACING_SECONDS)
        xml_text = self._get_statement(statement_url, reference_code)
        return parse_balances(xml_text)

    # --- step 1 ---

    def _send_request(self, query_id: str) -> tuple[str, str]:
        resp = self._session.get(
            SEND_REQUEST_URL,
            params={"t": self._token, "q": query_id, "v": API_VERSION},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        root = _parse_xml(resp.text)
        status = _text(root, "Status")
        if status and status.lower() != "success":
            _raise_for_flex_error(root)
        reference_code = _text(root, "ReferenceCode")
        url = _text(root, "Url")
        if not reference_code or not url:
            raise IbkrFlexError("unknown", "SendRequest returned no reference code/url")
        return reference_code, url

    # --- step 2 ---

    def _get_statement(self, url: str, reference_code: str) -> str:
        last_not_ready: Optional[IbkrReportNotReady] = None
        for attempt in range(_MAX_STATEMENT_ATTEMPTS):
            resp = self._session.get(
                url,
                params={"t": self._token, "q": reference_code, "v": API_VERSION},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            text = resp.text
            root = _parse_xml(text)
            # A FlexQueryResponse (the report) has no <Status>; a status wrapper
            # means an error or a "still generating" warning.
            if root.find("Status") is None and root.tag != "FlexStatementResponse":
                return text
            status = _text(root, "Status")
            if status and status.lower() == "success":
                return text
            try:
                _raise_for_flex_error(root)
            except IbkrReportNotReady as e:
                last_not_ready = e
                if attempt < _MAX_STATEMENT_ATTEMPTS - 1:
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                raise
        # Exhausted attempts on a not-ready report.
        assert last_not_ready is not None
        raise last_not_ready


# ─── XML helpers ────────────────────────────────────────────────────────────


def _parse_xml(text: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise IbkrFlexError("parse-error", f"Could not parse Flex response: {e}") from e


def _text(root: ET.Element, tag: str) -> Optional[str]:
    el = root.find(tag)
    if el is None or el.text is None:
        return None
    return el.text.strip()


def _raise_for_flex_error(root: ET.Element) -> None:
    code = _text(root, "ErrorCode") or "unknown"
    message = _text(root, "ErrorMessage") or "Flex request failed"
    if code == _IN_PROGRESS_CODE or "generation in progress" in message.lower():
        raise IbkrReportNotReady(code, message)
    # Token/query problems are auth-class (codes in the 1000s for bad token,
    # expired token, invalid query, etc.). Treat everything non-retryable here
    # as auth unless we can prove otherwise — callers only need retryable vs not.
    raise IbkrAuthError(code, message)


def _to_float(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_balances(xml_text: str) -> list[FetchedBalance]:
    """Extract balances from a FlexQueryResponse report.

    Pulls, per `<FlexStatement accountId=...>`:
      - net liquidation (total account value) in base currency, from the latest
        EquitySummaryByReportDateInBase `total` (or a `netLiquidation` attribute);
      - cash in base currency, from the EquitySummary `cash` or the CashReport's
        BASE_SUMMARY `endingCash`;
      - per-currency cash rows from the CashReport (informational, summary=False).
    """
    root = _parse_xml(xml_text)
    balances: list[FetchedBalance] = []
    for stmt in root.iter("FlexStatement"):
        account_id = stmt.get("accountId") or "unknown"
        as_of = stmt.get("whenGenerated") or stmt.get("toDate")
        base_ccy = _base_currency(stmt)
        alias = _acct_alias(stmt)
        balances.extend(_account_balances(stmt, account_id, base_ccy, as_of, alias))
    return balances


def _acct_alias(stmt: ET.Element) -> Optional[str]:
    """The user-set account alias, wherever the report carries it."""
    if stmt.get("acctAlias"):
        return stmt.get("acctAlias")
    info = stmt.find("AccountInformation")
    if info is not None and info.get("acctAlias"):
        return info.get("acctAlias")
    eq = _latest_equity_summary(stmt)
    if eq is not None and eq.get("acctAlias"):
        return eq.get("acctAlias")
    return None


def _base_currency(stmt: ET.Element) -> Optional[str]:
    info = stmt.find("AccountInformation")
    if info is not None and info.get("currency"):
        return info.get("currency")
    eq = _latest_equity_summary(stmt)
    if eq is not None and eq.get("currency"):
        return eq.get("currency")
    return None


def _latest_equity_summary(stmt: ET.Element) -> Optional[ET.Element]:
    rows = list(stmt.iter("EquitySummaryByReportDateInBase"))
    if not rows:
        return None
    return max(rows, key=lambda e: e.get("reportDate") or "")


def _account_balances(
    stmt: ET.Element,
    account_id: str,
    base_ccy: Optional[str],
    as_of: Optional[str],
    alias: Optional[str] = None,
) -> list[FetchedBalance]:
    out: list[FetchedBalance] = []
    equity = _latest_equity_summary(stmt)

    # Net liquidation (base currency).
    nav = None
    nav_ccy = base_ccy
    if equity is not None:
        nav = _to_float(equity.get("total")) or _to_float(equity.get("netLiquidation"))
    if nav is None:
        nav_el = next(
            (e for e in stmt.iter() if e.get("netLiquidation") is not None), None
        )
        if nav_el is not None:
            nav = _to_float(nav_el.get("netLiquidation"))
            nav_ccy = nav_el.get("currency") or base_ccy
    if nav is not None:
        out.append(
            FetchedBalance(
                ib_account_id=account_id,
                field=NET_LIQUIDATION,
                value=nav,
                currency=nav_ccy or "",
                as_of=as_of,
                summary=True,
                acct_alias=alias,
            )
        )

    # Cash (base currency) + per-currency breakdown.
    cash_rows = list(stmt.iter("CashReportCurrency"))
    base_cash = None
    if equity is not None:
        base_cash = _to_float(equity.get("cash"))
    for row in cash_rows:
        ccy = row.get("currency")
        amount = _to_float(row.get("endingCash"))
        if amount is None:
            amount = _to_float(row.get("totalCashValue"))
        if amount is None:
            continue
        if ccy in (None, "BASE_SUMMARY"):
            if base_cash is None:
                base_cash = amount
            continue
        out.append(
            FetchedBalance(
                ib_account_id=account_id,
                field=CASH,
                value=amount,
                currency=ccy,
                as_of=as_of,
                summary=False,
                acct_alias=alias,
            )
        )
    if base_cash is not None:
        out.append(
            FetchedBalance(
                ib_account_id=account_id,
                field=CASH,
                value=base_cash,
                currency=base_ccy or "",
                as_of=as_of,
                summary=True,
                acct_alias=alias,
            )
        )

    return out
