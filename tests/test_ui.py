"""Tests for the ui module's non-TTY fallback path.

When stdin/stdout aren't a TTY (pytest's captured stdio, CI, piped scripts),
ui must fall back to plain click-based rendering and prompts. These tests
exercise every helper that has a fallback branch so we catch regressions
without needing to render real Rich/questionary output.
"""
from __future__ import annotations

from ynab_reconciler import ui
from ynab_reconciler.api.models import Account
from ynab_reconciler.reconciler import AccountReconciliationResult


def _account(**overrides) -> Account:
    defaults = dict(
        id="acct-1",
        name="Checking",
        type="checking",
        on_budget=True,
        closed=False,
        balance=1_200_000,
        cleared_balance=1_200_000,
        uncleared_balance=0,
        deleted=False,
    )
    return Account(**{**defaults, **overrides})


def _result(acct: Account, *, adjustment: float, created: bool) -> AccountReconciliationResult:
    return AccountReconciliationResult(
        account=acct,
        ynab_balance_in_units=acct.cleared_balance_in_units(),
        statement_balance_in_units=acct.cleared_balance_in_units() + adjustment,
        adjustment_in_units=adjustment,
        adjustment_created=created,
    )


class TestIsInteractive:
    def test_returns_false_under_pytest_captured_stdio(self):
        # pytest captures stdio, so neither stdin nor stdout is a TTY.
        assert ui.is_interactive() is False


class TestFormatHelpers:
    def test_fmt_amount_thousands_separator(self):
        assert ui.fmt_amount(1234567) == "1,234,567"

    def test_money_positive_renders_value(self):
        text = ui.money(1500)
        assert "1,500" in text.plain

    def test_money_signed_positive_prefixes_plus(self):
        text = ui.money(1500, signed=True)
        assert text.plain.startswith("+")

    def test_money_negative_no_signed_prefix(self):
        text = ui.money(-1500, signed=True)
        assert text.plain.startswith("-")

    def test_status_badge_matched(self):
        acct = _account()
        text = ui.status_badge(_result(acct, adjustment=0.0, created=False))
        assert "matched" in text.plain

    def test_status_badge_adjusted(self):
        acct = _account()
        text = ui.status_badge(_result(acct, adjustment=500.0, created=True))
        assert "adjusted" in text.plain
        assert "+500" in text.plain

    def test_status_badge_discrepancy(self):
        acct = _account()
        text = ui.status_badge(_result(acct, adjustment=-300.0, created=False))
        assert "discrepancy" in text.plain
        assert "-300" in text.plain


class TestFallbackRendering:
    """Each helper must produce plain text and not raise under non-TTY stdio."""

    def test_banner_is_silent_when_not_interactive(self, capsys):
        ui.banner("My Plan", "HUF")
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_section_falls_back_to_plain_rule(self, capsys):
        ui.section("Accounts")
        out = capsys.readouterr().out
        assert "Accounts:" in out
        assert "─" in out

    def test_show_account_header_plain(self, capsys):
        acct = _account()
        ui.show_account_header(acct, "HUF")
        out = capsys.readouterr().out
        assert acct.name in out
        assert "YNAB cleared balance" in out
        assert "1,200" in out

    def test_show_account_header_includes_uncleared_when_nonzero(self, capsys):
        acct = _account(uncleared_balance=50_000)
        ui.show_account_header(acct, "HUF")
        out = capsys.readouterr().out
        assert "Uncleared" in out
        assert "50" in out

    def test_show_account_header_omits_uncleared_when_zero(self, capsys):
        acct = _account(uncleared_balance=0)
        ui.show_account_header(acct, "HUF")
        assert "Uncleared" not in capsys.readouterr().out

    def test_show_conversion_plain(self, capsys):
        ui.show_conversion(100.0, "EUR", 39_500.0, "HUF", 395.0)
        out = capsys.readouterr().out
        assert "EUR" in out and "HUF" in out
        assert "39,500" in out
        assert "395" in out

    def test_show_account_result_matched(self, capsys):
        acct = _account()
        ui.show_account_result(_result(acct, adjustment=0.0, created=False), no_adjust=False)
        assert "Balances match" in capsys.readouterr().out

    def test_show_account_result_adjusted(self, capsys):
        acct = _account()
        ui.show_account_result(_result(acct, adjustment=750.0, created=True), no_adjust=False)
        out = capsys.readouterr().out
        assert "Adjustment created" in out
        assert "+750" in out

    def test_show_account_result_discrepancy_no_adjust(self, capsys):
        acct = _account()
        ui.show_account_result(_result(acct, adjustment=-200.0, created=False), no_adjust=True)
        out = capsys.readouterr().out
        assert "Discrepancy" in out
        assert "-200" in out
        assert "--no-adjust" in out

    def test_show_skipped(self, capsys):
        ui.show_skipped()
        assert "Skipped" in capsys.readouterr().out

    def test_show_run_summary(self, capsys):
        acct = _account()
        results = [
            _result(acct, adjustment=0.0, created=False),
            _result(acct, adjustment=100.0, created=True),
            _result(acct, adjustment=-50.0, created=False),
        ]
        ui.show_run_summary(results)
        out = capsys.readouterr().out
        assert "1 matched" in out
        assert "1 adjusted" in out
        assert "1 discrepancies skipped" in out

    def test_show_run_summary_empty_is_silent(self, capsys):
        ui.show_run_summary([])
        assert capsys.readouterr().out == ""

    def test_show_error_writes_to_stderr(self, capsys):
        ui.show_error("kaboom")
        captured = capsys.readouterr()
        assert "kaboom" in captured.err

    def test_spinner_is_a_noop_context_manager(self, capsys):
        with ui.spinner("loading"):
            pass
        # No rich live display under non-TTY, so nothing is printed.
        assert capsys.readouterr().out == ""


class TestFallbackPrompts:
    def test_ask_statement_balance_empty_input_accepts_cleared(self, monkeypatch):
        monkeypatch.setattr("click.prompt", lambda *a, **kw: "")
        acct = _account()
        answer = ui.ask_statement_balance(acct)
        assert answer == (acct.cleared_balance_in_units(), None)

    def test_ask_statement_balance_quit(self, monkeypatch):
        monkeypatch.setattr("click.prompt", lambda *a, **kw: "q")
        assert ui.ask_statement_balance(_account()) == "quit"

    def test_ask_statement_balance_skip(self, monkeypatch):
        monkeypatch.setattr("click.prompt", lambda *a, **kw: "s")
        assert ui.ask_statement_balance(_account()) == "skip"

    def test_ask_statement_balance_parses_amount_with_iso(self, monkeypatch):
        monkeypatch.setattr("click.prompt", lambda *a, **kw: "1000 EUR")
        amount, iso = ui.ask_statement_balance(_account())
        assert amount == 1000.0
        assert iso == "EUR"

    def test_ask_statement_balance_parses_ft_as_huf(self, monkeypatch):
        monkeypatch.setattr("click.prompt", lambda *a, **kw: "1000 Ft")
        amount, iso = ui.ask_statement_balance(_account())
        assert amount == 1000.0
        assert iso == "HUF"

    def test_ask_statement_balance_invalid_falls_back_to_skip(self, monkeypatch, capsys):
        monkeypatch.setattr("click.prompt", lambda *a, **kw: "not a number")
        assert ui.ask_statement_balance(_account()) == "skip"
        assert "Invalid amount" in capsys.readouterr().err

    def test_confirm_large_adjustment_emits_warning_and_asks(self, monkeypatch, capsys):
        seen = {}

        def fake_confirm(text, default=False):
            seen["text"] = text
            seen["default"] = default
            return True

        monkeypatch.setattr("click.confirm", fake_confirm)
        assert ui.confirm_large_adjustment(adjustment=600.0, current=1000.0) is True
        out = capsys.readouterr().out
        assert "Large adjustment" in out
        assert "+600" in out
        assert "60%" in out
        assert seen["default"] is False

    def test_confirm_large_adjustment_returns_false_when_declined(self, monkeypatch):
        monkeypatch.setattr("click.confirm", lambda *a, **kw: False)
        assert ui.confirm_large_adjustment(adjustment=-400.0, current=1000.0) is False


class TestSelectAccountFallback:
    def test_returns_all_for_default_input(self, monkeypatch):
        monkeypatch.setattr("click.prompt", lambda *a, **kw: "")
        accts = [_account(id="a", name="A"), _account(id="b", name="B")]
        assert ui.select_account(accts, statuses={}) == "all"

    def test_returns_quit_for_q(self, monkeypatch):
        monkeypatch.setattr("click.prompt", lambda *a, **kw: "q")
        accts = [_account(id="a", name="A")]
        assert ui.select_account(accts, statuses={}) == "quit"

    def test_returns_account_id_for_index(self, monkeypatch):
        monkeypatch.setattr("click.prompt", lambda *a, **kw: "2")
        accts = [_account(id="a", name="A"), _account(id="b", name="B")]
        assert ui.select_account(accts, statuses={}) == "b"

    def test_renders_status_for_already_reconciled(self, monkeypatch, capsys):
        monkeypatch.setattr("click.prompt", lambda *a, **kw: "q")
        a = _account(id="a", name="A")
        b = _account(id="b", name="B")
        statuses = {"a": _result(a, adjustment=100.0, created=True)}
        ui.select_account([a, b], statuses=statuses)
        out = capsys.readouterr().out
        assert "[x]" in out  # reconciled marker for A
        assert "[ ]" in out  # pending marker for B
        assert "adjusted +100" in out

    def test_invalid_input_then_valid(self, monkeypatch):
        inputs = iter(["xyz", "99", "1"])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(inputs))
        accts = [_account(id="only", name="Only")]
        assert ui.select_account(accts, statuses={}) == "only"


class TestSelectPlanFallback:
    def test_picks_by_index(self, monkeypatch):
        from ynab_reconciler.api.models import Plan

        monkeypatch.setattr("click.prompt", lambda *a, **kw: "2")
        plans = [Plan(id="p1", name="One"), Plan(id="p2", name="Two")]
        assert ui.select_plan(plans).id == "p2"

    def test_default_picks_first(self, monkeypatch):
        from ynab_reconciler.api.models import Plan

        monkeypatch.setattr("click.prompt", lambda *a, **kw: "1")
        plans = [Plan(id="p1", name="One"), Plan(id="p2", name="Two")]
        assert ui.select_plan(plans).id == "p1"


class TestSelectCategoryGroupFallback:
    def test_picks_by_index(self, monkeypatch):
        from ynab_reconciler.api.models import CategoryGroup

        monkeypatch.setattr("click.prompt", lambda *a, **kw: "1")
        groups = [
            CategoryGroup(id="g1", name="Bills", hidden=False, deleted=False, categories=[]),
            CategoryGroup(id="g2", name="Fun", hidden=False, deleted=False, categories=[]),
        ]
        assert ui.select_category_group(groups).id == "g1"
