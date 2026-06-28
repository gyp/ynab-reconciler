"""Account connections: fetch real-world balances from external providers.

A *connection* is a configured, read-only remote source that can report an
account balance (the first being Interactive Brokers via the Flex Web Service).
Connections and their credentials are stored through `config.py` like everything
else; this package only holds the provider clients and the small registry that
maps a connection `type` to a fetch implementation. Nothing here imports
`keyring`/`tomllib` — secrets are passed in by the caller.
"""
from .errors import (
    ConnectionError,
    IbkrAuthError,
    IbkrFlexError,
    IbkrReportNotReady,
)
from .models import CASH, NET_LIQUIDATION, BALANCE_FIELDS, FetchedBalance
from .registry import (
    IBKR_FLEX,
    connection_types,
    fetch_balances,
    identifier_label,
    secret_label,
    select_balance,
    supports,
)

__all__ = [
    "ConnectionError",
    "IbkrFlexError",
    "IbkrAuthError",
    "IbkrReportNotReady",
    "FetchedBalance",
    "NET_LIQUIDATION",
    "CASH",
    "BALANCE_FIELDS",
    "IBKR_FLEX",
    "connection_types",
    "identifier_label",
    "secret_label",
    "fetch_balances",
    "select_balance",
    "supports",
]
