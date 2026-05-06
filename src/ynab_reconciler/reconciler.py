"""Reconciliation business logic."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from .api.client import YnabClient
from .api.models import Account, Category, ClearedStatus


@dataclass
class AccountReconciliationResult:
    account: Account
    ynab_balance_in_units: float
    statement_balance_in_units: float
    adjustment_in_units: float
    adjustment_created: bool

    @property
    def matched(self) -> bool:
        return abs(self.adjustment_in_units) < 0.001


def milliunits(amount: float) -> int:
    """Convert a currency unit amount to YNAB milliunits (thousandths of a currency unit)."""
    return round(amount * 1000)


def _build_subtransactions(amount_milliunits: int, categories: list[Category]) -> list[dict]:
    weights = [abs(cat.balance) for cat in categories]
    total_weight = sum(weights)

    if total_weight == 0:
        n = len(categories)
        base = amount_milliunits // n
        amounts = [base] * n
        amounts[-1] += amount_milliunits - sum(amounts)
    else:
        amounts = [round(amount_milliunits * w / total_weight) for w in weights[:-1]]
        amounts.append(amount_milliunits - sum(amounts))

    return [{"amount": amt, "category_id": cat.id} for amt, cat in zip(amounts, categories)]


def reconcile_account(
    client: YnabClient,
    plan_id: str,
    account: Account,
    statement_balance_in_units: float,
    create_adjustment: bool = True,
    payee_id: Optional[str] = None,
    categories: Optional[list[Category]] = None,
    memo: Optional[str] = None,
) -> AccountReconciliationResult:
    """
    Compare YNAB cleared balance against the user-supplied statement balance.
    If they differ and create_adjustment is True, post an adjustment transaction.
    """
    ynab_balance = account.cleared_balance_in_units()
    difference = statement_balance_in_units - ynab_balance

    adjustment_created = False
    if create_adjustment and abs(difference) >= 0.001:
        today = date.today().isoformat()
        amount_mu = milliunits(difference)
        subtransactions = _build_subtransactions(amount_mu, categories) if categories else None
        client.create_reconciliation_transaction(
            plan_id, account.id, amount_mu, today,
            payee_id=payee_id, subtransactions=subtransactions, memo=memo,
        )
        adjustment_created = True

    return AccountReconciliationResult(
        account=account,
        ynab_balance_in_units=ynab_balance,
        statement_balance_in_units=statement_balance_in_units,
        adjustment_in_units=difference,
        adjustment_created=adjustment_created,
    )
