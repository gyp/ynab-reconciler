"""Tests for ynab_reconciler.main.

Focused on `resolve_plan`, which holds the perf-sensitive logic for the
reconcile startup path. The structural guarantee that we no longer call the
heavy `/plans/{plan_id}` endpoint is enforced by the absence of
`YnabClient.get_plan` (see test_no_full_plan_export_method).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import click
import pytest

from ynab_reconciler import main
from ynab_reconciler.api.client import YnabClient
from ynab_reconciler.api.models import Plan


def _plan(plan_id: str, name: str = "My Budget", iso: str = "USD") -> Plan:
    return Plan(id=plan_id, name=name, iso_code=iso)


class TestResolvePlan:
    def test_returns_matching_plan_when_id_configured(self):
        client = MagicMock()
        client.get_plans.return_value = [_plan("p1", "Personal"), _plan("p2", "Work", "EUR")]

        result = main.resolve_plan(client, "p2")

        assert result.id == "p2"
        assert result.name == "Work"
        assert result.iso_code == "EUR"
        client.get_plans.assert_called_once()

    def test_raises_when_configured_id_not_in_list(self):
        client = MagicMock()
        client.get_plans.return_value = [_plan("p1"), _plan("p2")]

        with pytest.raises(click.UsageError, match="not found on this YNAB account"):
            main.resolve_plan(client, "nope")

    def test_raises_when_no_plans_at_all(self):
        client = MagicMock()
        client.get_plans.return_value = []

        with pytest.raises(click.UsageError, match="No plans found"):
            main.resolve_plan(client, "p1")

    def test_non_tty_without_plan_id_raises_before_api_call(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        with pytest.raises(click.UsageError, match="not running interactively"):
            main.resolve_plan(client, None)

        client.get_plans.assert_not_called()


class TestNoFullPlanExportMethod:
    """Structural pin: the heavy `/plans/{plan_id}` endpoint must stay gone.

    The reconcile startup used to call `client.get_plan(plan_id)` which
    downloads every transaction in the budget — multi-MB for a non-trivial
    plan. The method has been deleted from the client; this test makes the
    constraint explicit so a future re-introduction trips a clear failure
    here rather than silently regressing startup time.
    """

    def test_client_has_no_get_plan_method(self):
        assert not hasattr(YnabClient, "get_plan")


# ─── Connection balance pre-fill in the reconcile flow ───────────────────────

import json

from click.testing import CliRunner

from ynab_reconciler import config, connections
from ynab_reconciler.api.models import Account
from ynab_reconciler.connections.models import FetchedBalance


def _account(account_id: str = "acct-1", name: str = "Brokerage") -> Account:
    return Account(
        id=account_id,
        name=name,
        type="otherAsset",
        on_budget=True,
        closed=False,
        balance=0,
        cleared_balance=0,
        uncleared_balance=0,
        deleted=False,
    )


class TestConnectionDefault:
    def test_returns_prefill_for_paired_account(self, monkeypatch):
        monkeypatch.setattr(
            main.config, "pairing_for", lambda aid: config.Pairing("ibkr", "U1")
        )
        monkeypatch.setattr(
            main.config, "get_connection", lambda n: config.Connection("ibkr-flex", "q1")
        )
        monkeypatch.setattr(main.config, "get_connection_secret", lambda n: "tok")
        monkeypatch.setattr(
            main.connections,
            "fetch_balances",
            lambda conn, secret: [FetchedBalance("U1", "net_liquidation", 50000.0, "USD")],
        )

        cache: dict = {}
        result = main._connection_default(_account(), cache)

        assert result == (50000.0, "USD", "ibkr net liquidation")
        assert "ibkr" in cache  # cached for the rest of the run

    def test_none_when_account_not_paired(self, monkeypatch):
        monkeypatch.setattr(main.config, "pairing_for", lambda aid: None)
        assert main._connection_default(_account(), {}) is None

    def test_warns_and_returns_none_on_fetch_error(self, monkeypatch):
        monkeypatch.setattr(
            main.config, "pairing_for", lambda aid: config.Pairing("ibkr", "U1")
        )
        monkeypatch.setattr(
            main.config, "get_connection", lambda n: config.Connection("ibkr-flex", "q1")
        )
        monkeypatch.setattr(main.config, "get_connection_secret", lambda n: "tok")

        def boom(conn, secret):
            raise connections.IbkrReportNotReady("1019", "in progress")

        monkeypatch.setattr(main.connections, "fetch_balances", boom)
        assert main._connection_default(_account(), {}) is None

    def test_none_when_no_matching_balance(self, monkeypatch):
        monkeypatch.setattr(
            main.config, "pairing_for", lambda aid: config.Pairing("ibkr", "U-OTHER")
        )
        monkeypatch.setattr(
            main.config, "get_connection", lambda n: config.Connection("ibkr-flex", "q1")
        )
        monkeypatch.setattr(main.config, "get_connection_secret", lambda n: "tok")
        monkeypatch.setattr(
            main.connections,
            "fetch_balances",
            lambda conn, secret: [FetchedBalance("U1", "net_liquidation", 1.0, "USD")],
        )
        assert main._connection_default(_account(), {}) is None


class TestFetchCached:
    def test_fetches_once_per_run(self, monkeypatch):
        calls = []

        def fake_fetch(conn, secret):
            calls.append(1)
            return [FetchedBalance("U1", "net_liquidation", 1.0, "USD")]

        monkeypatch.setattr(main.config, "get_connection_secret", lambda n: "tok")
        monkeypatch.setattr(main.connections, "fetch_balances", fake_fetch)

        cache: dict = {}
        conn = config.Connection("ibkr-flex", "q1")
        main._fetch_cached("ibkr", conn, cache)
        main._fetch_cached("ibkr", conn, cache)
        assert len(calls) == 1

    def test_raises_without_stored_secret(self, monkeypatch):
        monkeypatch.setattr(main.config, "get_connection_secret", lambda n: None)
        with pytest.raises(connections.ConnectionError, match="No stored credential"):
            main._fetch_cached("ibkr", config.Connection("ibkr-flex", "q1"), {})


class TestConnectionsCli:
    def test_list_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        res = CliRunner().invoke(main.cli, ["connections", "list"])
        assert res.exit_code == 0
        assert "No connections configured" in res.output

    def test_list_shows_connection_and_pairing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(main.config, "get_connection_secret", lambda n: None)
        config.save_config(
            config.Config(
                connections={"ibkr": config.Connection("ibkr-flex", "q9")},
                pairings={"acct-1": config.Pairing("ibkr", "U1", "cash")},
            )
        )
        res = CliRunner().invoke(main.cli, ["connections", "list"])
        assert res.exit_code == 0
        assert "ibkr" in res.output
        assert "q9" in res.output
        assert "U1" in res.output

    def test_fetch_json(self, monkeypatch):
        monkeypatch.setattr(
            main.config, "get_connection", lambda n: config.Connection("ibkr-flex", "q1")
        )
        monkeypatch.setattr(main.config, "get_connection_secret", lambda n: "tok")
        monkeypatch.setattr(
            main.connections,
            "fetch_balances",
            lambda conn, secret: [
                FetchedBalance("U1", "net_liquidation", 50000.0, "USD", "20240201")
            ],
        )
        res = CliRunner().invoke(main.cli, ["connections", "fetch", "ibkr", "--json"])
        assert res.exit_code == 0
        data = json.loads(res.output)
        assert data[0]["value"] == 50000.0
        assert data[0]["ib_account_id"] == "U1"

    def test_fetch_unknown_connection_exits_nonzero(self, monkeypatch):
        monkeypatch.setattr(main.config, "get_connection", lambda n: None)
        res = CliRunner().invoke(main.cli, ["connections", "fetch", "nope"])
        assert res.exit_code == 1


class TestBrokerAccountChoices:
    def _rep(self):
        FB = FetchedBalance
        return {
            "U16045816": FB("U16045816", "net_liquidation", 5825927.34, "HUF", acct_alias="TBSZ-2024"),
            "U16102844": FB("U16102844", "net_liquidation", 16973.80, "EUR", acct_alias=None),
            "U19162691": FB("U19162691", "net_liquidation", 2157814.68, "HUF", acct_alias="TBSZ-2025"),
        }

    def test_converted_value_shown_only_for_differing_currency(self, monkeypatch):
        monkeypatch.setattr(main, "convert_currency", lambda a, f, t: a * 390.0)
        rep = self._rep()
        labels, _ = main._broker_account_choices(rep, sorted(rep), "HUF", 5_800_000.0)
        assert "≈" in labels["U16102844"] and "HUF" in labels["U16102844"]  # EUR → HUF
        assert "≈" not in labels["U16045816"]  # already HUF

    def test_columns_are_aligned(self, monkeypatch):
        monkeypatch.setattr(main, "convert_currency", lambda a, f, t: a * 390.0)
        rep = self._rep()
        labels, _ = main._broker_account_choices(rep, sorted(rep), "HUF", 5_800_000.0)
        bullet_cols = {lbl.index("·") for lbl in labels.values()}
        assert len(bullet_cols) == 1  # the '·' separator lines up across rows

    def test_preselects_closest_within_10pct(self, monkeypatch):
        monkeypatch.setattr(main, "convert_currency", lambda a, f, t: a * 390.0)
        rep = self._rep()
        _, default = main._broker_account_choices(rep, sorted(rep), "HUF", 5_800_000.0)
        assert default == "U16045816"

    def test_no_preselection_when_nothing_within_10pct(self, monkeypatch):
        monkeypatch.setattr(main, "convert_currency", lambda a, f, t: a * 390.0)
        rep = self._rep()
        _, default = main._broker_account_choices(rep, sorted(rep), "HUF", 1_000_000.0)
        assert default is None

    def test_conversion_failure_degrades(self, monkeypatch):
        def boom(a, f, t):
            raise ValueError("no rate")

        monkeypatch.setattr(main, "convert_currency", boom)
        rep = self._rep()
        labels, default = main._broker_account_choices(rep, sorted(rep), "HUF", 5_800_000.0)
        assert "≈" not in labels["U16102844"]  # no converted figure
        # EUR account isn't scorable, but the matching HUF one still preselects
        assert default == "U16045816"


from ynab_reconciler.reconciler import AccountReconciliationResult


class TestReconcileConnectionConversionDisplay:
    def test_converted_default_computed_once_and_reused(self, monkeypatch):
        acct = _account("acct-1", "Euro pot")  # cleared 0, budget HUF
        monkeypatch.setattr(
            main, "_connection_default",
            lambda a, cache: (1000.0, "EUR", "ibkr net liquidation"),
        )
        calls = []

        def fake_convert(amount, frm, to):
            calls.append((amount, frm, to))
            return amount * 390.0

        monkeypatch.setattr(main, "convert_currency", fake_convert)
        # Accept the prefilled default (non-TTY click.prompt returns its default).
        monkeypatch.setattr("click.prompt", lambda *a, **kw: kw.get("default", ""))

        result = AccountReconciliationResult(
            account=acct,
            ynab_balance_in_units=0.0,
            statement_balance_in_units=390000.0,
            adjustment_in_units=390000.0,
            adjustment_created=True,
        )
        monkeypatch.setattr(main, "reconcile_account", lambda *a, **k: result)

        status, _ = main._reconcile_one(
            MagicMock(), "plan", acct, "HUF", [], None, False, {}
        )

        assert status == "done"
        # Converted once for the prompt display and reused for the adjustment.
        assert calls == [(1000.0, "EUR", "HUF")]
