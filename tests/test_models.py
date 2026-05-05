from ynab_reconciler.api.models import Account, ClearedStatus, Transaction


def _account(**overrides) -> dict:
    defaults = {
        "id": "acct-1",
        "name": "Checking",
        "type": "checking",
        "on_budget": True,
        "closed": False,
        "balance": 1_500_000,
        "cleared_balance": 1_200_000,
        "uncleared_balance": 300_000,
        "deleted": False,
        "last_reconciled_at": None,
        "note": None,
    }
    return {**defaults, **overrides}


def _transaction(**overrides) -> dict:
    defaults = {
        "id": "txn-1",
        "date": "2024-01-15",
        "amount": -50_000,
        "cleared": "cleared",
        "approved": True,
        "account_id": "acct-1",
        "deleted": False,
    }
    return {**defaults, **overrides}


class TestAccount:
    def test_from_dict_basic(self):
        a = Account.from_dict(_account())
        assert a.id == "acct-1"
        assert a.name == "Checking"
        assert a.balance == 1_500_000
        assert a.deleted is False

    def test_balance_in_units(self):
        a = Account.from_dict(_account(balance=1_500_000))
        assert a.balance_in_units() == 1500.0

    def test_cleared_balance_in_units(self):
        a = Account.from_dict(_account(cleared_balance=1_200_000))
        assert a.cleared_balance_in_units() == 1200.0

    def test_uncleared_balance_in_units(self):
        a = Account.from_dict(_account(uncleared_balance=300_000))
        assert a.uncleared_balance_in_units() == 300.0

    def test_negative_balance(self):
        a = Account.from_dict(_account(balance=-750_000))
        assert a.balance_in_units() == -750.0


class TestTransaction:
    def test_from_dict_basic(self):
        t = Transaction.from_dict(_transaction())
        assert t.id == "txn-1"
        assert t.cleared == ClearedStatus.CLEARED
        assert t.amount == -50_000

    def test_amount_in_units(self):
        t = Transaction.from_dict(_transaction(amount=-50_000))
        assert t.amount_in_units() == -50.0

    def test_cleared_status_enum(self):
        for status in ("cleared", "uncleared", "reconciled"):
            t = Transaction.from_dict(_transaction(cleared=status))
            assert t.cleared == ClearedStatus(status)

    def test_optional_fields_default_none(self):
        t = Transaction.from_dict(_transaction())
        assert t.payee_id is None
        assert t.payee_name is None
        assert t.memo is None
