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
