"""YNAB API client."""
from __future__ import annotations

from typing import Optional

import requests

from .models import Account, CategoryGroup, ClearedStatus, Payee, Plan, Transaction

BASE_URL = "https://api.ynab.com/v1"


class YnabError(Exception):
    def __init__(self, name: str, detail: str) -> None:
        self.name = name
        self.detail = detail
        super().__init__(f"{name}: {detail}")


class YnabClient:
    def __init__(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"
        self._session.headers["Content-Type"] = "application/json"

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = self._session.get(f"{BASE_URL}{path}", params=params)
        return self._handle(resp)

    def _patch(self, path: str, body: dict) -> dict:
        resp = self._session.patch(f"{BASE_URL}{path}", json=body)
        return self._handle(resp)

    def _put(self, path: str, body: dict) -> dict:
        resp = self._session.put(f"{BASE_URL}{path}", json=body)
        return self._handle(resp)

    def _post(self, path: str, body: dict) -> dict:
        resp = self._session.post(f"{BASE_URL}{path}", json=body)
        return self._handle(resp)

    @staticmethod
    def _handle(resp: requests.Response) -> dict:
        data = resp.json()
        if not resp.ok:
            err = data.get("error", {})
            raise YnabError(err.get("name", "unknown"), err.get("detail", resp.text))
        return data

    # --- Plans ---

    def get_plans(self) -> list[Plan]:
        data = self._get("/plans")
        return [Plan.from_dict(p) for p in data["data"]["plans"]]

    # --- Payees ---

    def get_payees(self, plan_id: str) -> list[Payee]:
        data = self._get(f"/plans/{plan_id}/payees")
        return [Payee.from_dict(p) for p in data["data"]["payees"]]

    # --- Categories ---

    def get_category_groups(self, plan_id: str) -> list[CategoryGroup]:
        data = self._get(f"/plans/{plan_id}/categories")
        return [CategoryGroup.from_dict(g) for g in data["data"]["category_groups"]]

    # --- Accounts ---

    def get_accounts(self, plan_id: str) -> list[Account]:
        data = self._get(f"/plans/{plan_id}/accounts")
        return [Account.from_dict(a) for a in data["data"]["accounts"]]

    def get_account(self, plan_id: str, account_id: str) -> Account:
        data = self._get(f"/plans/{plan_id}/accounts/{account_id}")
        return Account.from_dict(data["data"]["account"])

    # --- Transactions ---

    def get_transactions(
        self,
        plan_id: str,
        account_id: str,
        since_date: Optional[str] = None,
    ) -> list[Transaction]:
        params: dict = {}
        if since_date:
            params["since_date"] = since_date
        data = self._get(f"/plans/{plan_id}/accounts/{account_id}/transactions", params=params)
        return [Transaction.from_dict(t) for t in data["data"]["transactions"]]

    def update_transaction_cleared(
        self,
        plan_id: str,
        transaction_id: str,
        cleared: ClearedStatus,
    ) -> Transaction:
        body = {"transaction": {"cleared": cleared.value}}
        data = self._put(f"/plans/{plan_id}/transactions/{transaction_id}", body)
        return Transaction.from_dict(data["data"]["transaction"])

    def bulk_update_cleared(
        self,
        plan_id: str,
        transaction_ids: list[str],
        cleared: ClearedStatus,
    ) -> list[str]:
        """Update cleared status on multiple transactions at once. Returns updated ids."""
        transactions = [
            {"id": tid, "cleared": cleared.value} for tid in transaction_ids
        ]
        data = self._patch(f"/plans/{plan_id}/transactions", {"transactions": transactions})
        return data["data"].get("transaction_ids", [])

    def create_reconciliation_transaction(
        self,
        plan_id: str,
        account_id: str,
        amount_milliunits: int,
        date: str,
        payee_id: Optional[str] = None,
        subtransactions: Optional[list[dict]] = None,
    ) -> Transaction:
        """Create a reconciliation adjustment transaction."""
        txn: dict = {
            "account_id": account_id,
            "date": date,
            "amount": amount_milliunits,
            "memo": "Reconciliation adjustment",
            "cleared": ClearedStatus.CLEARED.value,
            "approved": True,
        }
        if payee_id:
            txn["payee_id"] = payee_id
        if subtransactions:
            txn["subtransactions"] = subtransactions
        data = self._post(f"/plans/{plan_id}/transactions", {"transaction": txn})
        return Transaction.from_dict(data["data"]["transaction"])
