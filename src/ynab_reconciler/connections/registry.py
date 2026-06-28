"""Connection-type registry: the seam new providers plug into.

Maps a connection `type` string to a fetch implementation, and offers
`select_balance` to pick the figure a pairing points at. Adding a provider means
adding one entry here plus its client module — nothing else in the app needs to
know the concrete types.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

from .ibkr_flex import IbkrFlexClient
from .models import FetchedBalance

if TYPE_CHECKING:  # avoid importing config at runtime; keep this layer thin
    from ..config import Connection, Pairing

IBKR_FLEX = "ibkr-flex"

# Per-type human labels for the two pieces of input `connections add` collects:
# the report/query identifier (stored in config) and the access token (kept in
# the keychain). New providers add an entry here so prompts read naturally;
# callers fall back to the generic label for unknown types.
_IDENTIFIER_LABELS = {IBKR_FLEX: "IBKR Flex query id"}
_SECRET_LABELS = {IBKR_FLEX: "IBKR Flex token"}


def identifier_label(conn_type: str) -> str:
    """Human name for a connection type's report/query identifier."""
    return _IDENTIFIER_LABELS.get(conn_type, "report/query id")


def secret_label(conn_type: str) -> str:
    """Human name for a connection type's access token/secret."""
    return _SECRET_LABELS.get(conn_type, "access token")


def _fetch_ibkr_flex(query_id: str, secret: str) -> list[FetchedBalance]:
    return IbkrFlexClient(secret).fetch_balances(query_id)


# type -> (query_id, secret) -> balances
_FETCHERS: dict[str, Callable[[str, str], list[FetchedBalance]]] = {
    IBKR_FLEX: _fetch_ibkr_flex,
}


def supports(conn_type: str) -> bool:
    return conn_type in _FETCHERS


def connection_types() -> tuple[str, ...]:
    return tuple(_FETCHERS)


def fetch_balances(connection: "Connection", secret: str) -> list[FetchedBalance]:
    """Fetch balances for a configured connection using its stored secret."""
    fetcher = _FETCHERS.get(connection.type)
    if fetcher is None:
        raise ValueError(f"Unknown connection type: {connection.type!r}")
    return fetcher(connection.query_id, secret)


def select_balance(
    balances: list[FetchedBalance], pairing: "Pairing"
) -> Optional[FetchedBalance]:
    """Pick the balance a pairing refers to: matching account + field, preferring
    the headline base-currency (summary) figure when several match."""
    matches = [
        b
        for b in balances
        if b.ib_account_id == pairing.ib_account_id and b.field == pairing.field
    ]
    if not matches:
        return None
    return next((b for b in matches if b.summary), matches[0])
