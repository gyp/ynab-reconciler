"""Value types shared across connection providers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Balance figures a pairing can select. Net liquidation (total account value) is
# the default — for an investment account reconciled against a statement total
# it's the right number; cash is the cash-only portion.
NET_LIQUIDATION = "net_liquidation"
CASH = "cash"
BALANCE_FIELDS = (NET_LIQUIDATION, CASH)


@dataclass
class FetchedBalance:
    """A single balance figure pulled from a connection.

    `value` is in plain currency units (e.g. dollars), NOT YNAB milliunits —
    conversion to milliunits happens only at the YNAB boundary. `summary` marks
    the headline base-currency figure for an (account, field) pair; non-summary
    rows (e.g. per-currency cash breakdowns) are informational only and are not
    used to pre-fill the reconcile prompt.
    """

    ib_account_id: str
    field: str
    value: float
    currency: str
    as_of: Optional[str] = None
    summary: bool = True
    acct_alias: Optional[str] = None

    def account_label(self) -> str:
        """Account identity for display, e.g. 'TBSZ-2024 (U16045816)' or 'U123'."""
        if self.acct_alias:
            return f"{self.acct_alias} ({self.ib_account_id})"
        return self.ib_account_id

    def label(self) -> str:
        """Human-readable one-liner, e.g. 'TBSZ-2024 (U16045816)  net liquidation  …'."""
        pretty_field = self.field.replace("_", " ")
        return f"{self.account_label()}  {pretty_field}  {self.value:,.2f} {self.currency}"
