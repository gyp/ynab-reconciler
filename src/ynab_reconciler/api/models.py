"""Dataclasses for YNAB API entities."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class ClearedStatus(str, Enum):
    CLEARED = "cleared"
    UNCLEARED = "uncleared"
    RECONCILED = "reconciled"


class AccountType(str, Enum):
    CHECKING = "checking"
    SAVINGS = "savings"
    CASH = "cash"
    CREDIT_CARD = "creditCard"
    LINE_OF_CREDIT = "lineOfCredit"
    OTHER_ASSET = "otherAsset"
    OTHER_LIABILITY = "otherLiability"
    MORTGAGE = "mortgage"
    AUTO_LOAN = "autoLoan"
    STUDENT_LOAN = "studentLoan"
    PERSONAL_LOAN = "personalLoan"
    MEDICAL_DEBT = "medicalDebt"
    OTHER_DEBT = "otherDebt"


@dataclass
class Plan:
    id: str
    name: str
    last_modified_on: Optional[str] = None
    first_month: Optional[str] = None
    last_month: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        return cls(
            id=data["id"],
            name=data["name"],
            last_modified_on=data.get("last_modified_on"),
            first_month=data.get("first_month"),
            last_month=data.get("last_month"),
        )


@dataclass
class Account:
    id: str
    name: str
    type: str
    on_budget: bool
    closed: bool
    balance: int
    cleared_balance: int
    uncleared_balance: int
    deleted: bool
    last_reconciled_at: Optional[str] = None
    note: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "Account":
        return cls(
            id=data["id"],
            name=data["name"],
            type=data["type"],
            on_budget=data["on_budget"],
            closed=data["closed"],
            balance=data["balance"],
            cleared_balance=data["cleared_balance"],
            uncleared_balance=data["uncleared_balance"],
            deleted=data["deleted"],
            last_reconciled_at=data.get("last_reconciled_at"),
            note=data.get("note"),
        )

    def balance_in_units(self) -> float:
        return self.balance / 1000

    def cleared_balance_in_units(self) -> float:
        return self.cleared_balance / 1000

    def uncleared_balance_in_units(self) -> float:
        return self.uncleared_balance / 1000


@dataclass
class Payee:
    id: str
    name: str
    deleted: bool
    transfer_account_id: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "Payee":
        return cls(
            id=data["id"],
            name=data["name"],
            deleted=data["deleted"],
            transfer_account_id=data.get("transfer_account_id"),
        )


@dataclass
class Category:
    id: str
    name: str
    category_group_id: str
    hidden: bool
    deleted: bool
    balance: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "Category":
        return cls(
            id=data["id"],
            name=data["name"],
            category_group_id=data["category_group_id"],
            hidden=data["hidden"],
            deleted=data["deleted"],
            balance=data.get("balance", 0),
        )


@dataclass
class CategoryGroup:
    id: str
    name: str
    hidden: bool
    deleted: bool
    categories: list[Category] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "CategoryGroup":
        cats = [Category.from_dict(c) for c in data.get("categories", [])]
        return cls(
            id=data["id"],
            name=data["name"],
            hidden=data["hidden"],
            deleted=data["deleted"],
            categories=cats,
        )


@dataclass
class Transaction:
    id: str
    date: str
    amount: int
    cleared: ClearedStatus
    approved: bool
    account_id: str
    deleted: bool
    payee_id: Optional[str] = None
    payee_name: Optional[str] = None
    category_id: Optional[str] = None
    category_name: Optional[str] = None
    memo: Optional[str] = None
    import_id: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "Transaction":
        return cls(
            id=data["id"],
            date=data["date"],
            amount=data["amount"],
            cleared=ClearedStatus(data["cleared"]),
            approved=data["approved"],
            account_id=data["account_id"],
            deleted=data["deleted"],
            payee_id=data.get("payee_id"),
            payee_name=data.get("payee_name"),
            category_id=data.get("category_id"),
            category_name=data.get("category_name"),
            memo=data.get("memo"),
            import_id=data.get("import_id"),
        )

    def amount_in_units(self) -> float:
        return self.amount / 1000
