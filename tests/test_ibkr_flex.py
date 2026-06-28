"""Tests for the Interactive Brokers Flex Web Service client.

The live API is never hit: the two HTTP calls are mocked via an injected
requests.Session, and time.sleep is patched out so retries don't actually wait.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ynab_reconciler.connections import ibkr_flex
from ynab_reconciler.connections.errors import IbkrAuthError, IbkrReportNotReady
from ynab_reconciler.connections.models import CASH, NET_LIQUIDATION

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def _resp(text: str) -> MagicMock:
    r = MagicMock()
    r.text = text
    r.raise_for_status = MagicMock()
    return r


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ibkr_flex.time, "sleep", lambda *_: None)


# ─── parsing ─────────────────────────────────────────────────────────────────


class TestParseBalances:
    def test_net_liquidation_uses_latest_report_date(self):
        balances = ibkr_flex.parse_balances(_fixture("ibkr_statement_ready.xml"))
        nav = [
            b for b in balances
            if b.ib_account_id == "U1111111" and b.field == NET_LIQUIDATION
        ]
        assert len(nav) == 1
        assert nav[0].value == 50000.00  # 20240131, not 20240130
        assert nav[0].currency == "USD"
        assert nav[0].summary is True

    def test_summary_cash_in_base_currency(self):
        balances = ibkr_flex.parse_balances(_fixture("ibkr_statement_ready.xml"))
        cash = [
            b for b in balances
            if b.ib_account_id == "U1111111" and b.field == CASH and b.summary
        ]
        assert len(cash) == 1
        assert cash[0].value == 12000.00
        assert cash[0].currency == "USD"

    def test_per_currency_cash_rows_are_non_summary(self):
        balances = ibkr_flex.parse_balances(_fixture("ibkr_statement_ready.xml"))
        per_ccy = {
            b.currency: b
            for b in balances
            if b.ib_account_id == "U1111111" and b.field == CASH and not b.summary
        }
        assert per_ccy["USD"].value == 10000.00
        assert per_ccy["EUR"].value == 1800.00

    def test_multiple_accounts_and_currencies(self):
        balances = ibkr_flex.parse_balances(_fixture("ibkr_statement_ready.xml"))
        eur_nav = [
            b for b in balances
            if b.ib_account_id == "U2222222" and b.field == NET_LIQUIDATION
        ]
        assert eur_nav[0].value == 25000.00
        assert eur_nav[0].currency == "EUR"

    def test_as_of_is_captured(self):
        balances = ibkr_flex.parse_balances(_fixture("ibkr_statement_ready.xml"))
        assert all(b.as_of == "20240201;120000" for b in balances)

    def test_acct_alias_captured(self):
        balances = ibkr_flex.parse_balances(_fixture("ibkr_statement_ready.xml"))
        aliased = [b for b in balances if b.ib_account_id == "U1111111"]
        assert all(b.acct_alias == "Main-Brokerage" for b in aliased)
        # account_label combines alias + id; falls back to id when no alias.
        assert aliased[0].account_label() == "Main-Brokerage (U1111111)"
        no_alias = next(b for b in balances if b.ib_account_id == "U2222222")
        assert no_alias.acct_alias is None
        assert no_alias.account_label() == "U2222222"


# ─── two-step flow ───────────────────────────────────────────────────────────


class TestFetchFlow:
    def test_happy_path_runs_two_calls(self):
        session = MagicMock()
        session.get.side_effect = [
            _resp(_fixture("ibkr_send_request_success.xml")),
            _resp(_fixture("ibkr_statement_ready.xml")),
        ]
        client = ibkr_flex.IbkrFlexClient("SECRET-TOKEN", session=session)

        balances = client.fetch_balances("Q1")

        assert session.get.call_count == 2
        first = session.get.call_args_list[0]
        assert first.args[0] == ibkr_flex.SEND_REQUEST_URL
        assert first.kwargs["params"] == {"t": "SECRET-TOKEN", "q": "Q1", "v": "3"}
        second = session.get.call_args_list[1]
        assert second.args[0].endswith("GetStatement")
        assert second.kwargs["params"] == {
            "t": "SECRET-TOKEN",
            "q": "1234567890",
            "v": "3",
        }
        assert any(b.field == NET_LIQUIDATION for b in balances)

    def test_retries_until_report_ready(self):
        session = MagicMock()
        session.get.side_effect = [
            _resp(_fixture("ibkr_send_request_success.xml")),
            _resp(_fixture("ibkr_statement_in_progress.xml")),
            _resp(_fixture("ibkr_statement_in_progress.xml")),
            _resp(_fixture("ibkr_statement_ready.xml")),
        ]
        client = ibkr_flex.IbkrFlexClient("SECRET", session=session)

        balances = client.fetch_balances("Q1")

        assert session.get.call_count == 4
        assert balances

    def test_gives_up_after_max_attempts(self):
        session = MagicMock()
        session.get.side_effect = [
            _resp(_fixture("ibkr_send_request_success.xml")),
            *[_resp(_fixture("ibkr_statement_in_progress.xml"))]
            * ibkr_flex._MAX_STATEMENT_ATTEMPTS,
        ]
        client = ibkr_flex.IbkrFlexClient("SECRET", session=session)

        with pytest.raises(IbkrReportNotReady):
            client.fetch_balances("Q1")

    def test_send_request_failure_raises_auth_error(self):
        session = MagicMock()
        session.get.side_effect = [_resp(_fixture("ibkr_send_request_fail.xml"))]
        client = ibkr_flex.IbkrFlexClient("SECRET", session=session)

        with pytest.raises(IbkrAuthError) as exc:
            client.fetch_balances("Q1")
        assert exc.value.code == "1012"
        # step 2 must not run after a step-1 failure
        assert session.get.call_count == 1

    def test_token_never_appears_in_errors(self):
        session = MagicMock()
        session.get.side_effect = [_resp(_fixture("ibkr_send_request_fail.xml"))]
        client = ibkr_flex.IbkrFlexClient("super-secret-token", session=session)

        with pytest.raises(IbkrAuthError) as exc:
            client.fetch_balances("Q1")
        assert "super-secret-token" not in str(exc.value)
