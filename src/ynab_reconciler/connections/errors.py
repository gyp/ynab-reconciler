"""Exceptions for account connections.

These deliberately never carry the access token in their messages — callers
surface them to the user via `ui.show_error`, and a token must never leak into
logs or terminal output.
"""
from __future__ import annotations


class ConnectionError(Exception):
    """Base class for all connection-fetch failures."""


class IbkrFlexError(ConnectionError):
    """An error reported by the IBKR Flex Web Service.

    `code` is the Flex `ErrorCode`; `detail` the `ErrorMessage`."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"IBKR Flex error {code}: {detail}")


class IbkrAuthError(IbkrFlexError):
    """The Flex token or query id was rejected (not retryable)."""


class IbkrReportNotReady(IbkrFlexError):
    """The statement is still being generated; retrying shortly may succeed."""
