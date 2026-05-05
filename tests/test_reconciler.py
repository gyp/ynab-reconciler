from unittest.mock import MagicMock

from ynab_reconciler.api.models import Account, Category, ClearedStatus
from ynab_reconciler.reconciler import _build_subtransactions, milliunits, reconcile_account


def _make_account(**overrides) -> Account:
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


class TestMilliunits:
    def test_positive(self):
        assert milliunits(1.50) == 1500

    def test_negative(self):
        assert milliunits(-10.0) == -10_000

    def test_zero(self):
        assert milliunits(0.0) == 0

    def test_rounds(self):
        assert milliunits(0.0015) == 2


class TestReconcileAccount:
    def test_no_adjustment_when_balances_match(self):
        client = MagicMock()
        account = _make_account(cleared_balance=1_200_000)

        result = reconcile_account(client, "plan-1", account, 1200.0)

        assert result.matched is True
        assert result.adjustment_created is False
        client.create_reconciliation_transaction.assert_not_called()

    def test_creates_positive_adjustment_when_ynab_is_low(self):
        client = MagicMock()
        account = _make_account(cleared_balance=1_000_000)

        result = reconcile_account(client, "plan-1", account, 1200.0)

        assert result.adjustment_created is True
        assert abs(result.adjustment_in_units - 200.0) < 0.001
        client.create_reconciliation_transaction.assert_called_once_with(
            "plan-1", "acct-1", 200_000, __import__("datetime").date.today().isoformat(),
            payee_id=None, subtransactions=None,
        )

    def test_creates_negative_adjustment_when_ynab_is_high(self):
        client = MagicMock()
        account = _make_account(cleared_balance=1_400_000)

        result = reconcile_account(client, "plan-1", account, 1200.0)

        assert result.adjustment_created is True
        assert abs(result.adjustment_in_units - (-200.0)) < 0.001

    def test_skips_adjustment_when_disabled(self):
        client = MagicMock()
        account = _make_account(cleared_balance=1_000_000)

        result = reconcile_account(
            client, "plan-1", account, 1200.0, create_adjustment=False
        )

        assert result.adjustment_created is False
        assert result.matched is False
        client.create_reconciliation_transaction.assert_not_called()

    def test_matched_property_true_on_small_float_difference(self):
        client = MagicMock()
        # 1200.0000001 dollars — well under the 0.001 threshold
        account = _make_account(cleared_balance=1_200_000)

        result = reconcile_account(client, "plan-1", account, 1200.0000001)

        assert result.matched is True

    def test_ynab_balance_dollars_captured_correctly(self):
        client = MagicMock()
        account = _make_account(cleared_balance=750_500)

        result = reconcile_account(client, "plan-1", account, 750.50)

        assert abs(result.ynab_balance_in_units - 750.50) < 0.001


def _cat(id: str, balance: int) -> Category:
    return Category(
        id=id,
        name=id,
        category_group_id="group-1",
        hidden=False,
        deleted=False,
        balance=balance,
    )


class TestBuildSubtransactions:
    def test_single_category_gets_full_amount(self):
        cats = [_cat("a", 5_000_000)]
        result = _build_subtransactions(300_000, cats)
        assert result == [{"amount": 300_000, "category_id": "a"}]

    def test_single_category_negative_amount(self):
        cats = [_cat("a", 5_000_000)]
        result = _build_subtransactions(-300_000, cats)
        assert result == [{"amount": -300_000, "category_id": "a"}]

    def test_two_equal_balances_split_evenly(self):
        cats = [_cat("a", 1_000_000), _cat("b", 1_000_000)]
        result = _build_subtransactions(200_000, cats)
        assert result == [
            {"amount": 100_000, "category_id": "a"},
            {"amount": 100_000, "category_id": "b"},
        ]

    def test_two_categories_1_to_2_ratio(self):
        cats = [_cat("a", 1_000_000), _cat("b", 2_000_000)]
        result = _build_subtransactions(300_000, cats)
        assert result == [
            {"amount": 100_000, "category_id": "a"},
            {"amount": 200_000, "category_id": "b"},
        ]

    def test_two_categories_1_to_2_ratio_negative_adjustment(self):
        cats = [_cat("a", 1_000_000), _cat("b", 2_000_000)]
        result = _build_subtransactions(-300_000, cats)
        assert result == [
            {"amount": -100_000, "category_id": "a"},
            {"amount": -200_000, "category_id": "b"},
        ]

    def test_negative_balances_use_absolute_values(self):
        cats = [_cat("a", -1_000_000), _cat("b", -2_000_000)]
        result = _build_subtransactions(300_000, cats)
        assert result == [
            {"amount": 100_000, "category_id": "a"},
            {"amount": 200_000, "category_id": "b"},
        ]

    def test_mixed_sign_balances_use_absolute_values(self):
        cats = [_cat("a", -1_000_000), _cat("b", 2_000_000)]
        result = _build_subtransactions(300_000, cats)
        assert result == [
            {"amount": 100_000, "category_id": "a"},
            {"amount": 200_000, "category_id": "b"},
        ]

    def test_all_zero_balances_split_equally(self):
        cats = [_cat("a", 0), _cat("b", 0), _cat("c", 0)]
        result = _build_subtransactions(300_000, cats)
        assert result == [
            {"amount": 100_000, "category_id": "a"},
            {"amount": 100_000, "category_id": "b"},
            {"amount": 100_000, "category_id": "c"},
        ]

    def test_all_zero_balances_remainder_goes_to_last(self):
        # 100_001 // 3 = 33_333 remainder 2, so last gets 33_333 + 2 = 33_335
        cats = [_cat("a", 0), _cat("b", 0), _cat("c", 0)]
        result = _build_subtransactions(100_001, cats)
        amounts = [s["amount"] for s in result]
        assert sum(amounts) == 100_001
        assert amounts[0] == 33_333
        assert amounts[1] == 33_333
        assert amounts[2] == 33_335

    def test_zero_balance_category_gets_nothing(self):
        cats = [_cat("a", 0), _cat("b", 1_000_000)]
        result = _build_subtransactions(300_000, cats)
        assert result == [
            {"amount": 0, "category_id": "a"},
            {"amount": 300_000, "category_id": "b"},
        ]

    def test_three_categories_1_2_3_ratio(self):
        cats = [_cat("a", 1_000_000), _cat("b", 2_000_000), _cat("c", 3_000_000)]
        result = _build_subtransactions(600_000, cats)
        assert result == [
            {"amount": 100_000, "category_id": "a"},
            {"amount": 200_000, "category_id": "b"},
            {"amount": 300_000, "category_id": "c"},
        ]

    def test_rounding_last_category_absorbs_remainder(self):
        # 1:2 ratio, total=100 → a gets round(33.33)=33, b gets 100-33=67
        cats = [_cat("a", 1_000), _cat("b", 2_000)]
        result = _build_subtransactions(100, cats)
        assert result[0]["amount"] == 33
        assert result[1]["amount"] == 67
        assert sum(s["amount"] for s in result) == 100

    def test_rounding_negative_adjustment_exact_sum(self):
        cats = [_cat("a", 1_000), _cat("b", 2_000)]
        result = _build_subtransactions(-100, cats)
        assert result[0]["amount"] == -33
        assert result[1]["amount"] == -67
        assert sum(s["amount"] for s in result) == -100

    def test_amounts_always_sum_to_total(self):
        cases = [
            ([1_000_000, 3_000_000], 777_777),
            ([500_000, 500_000, 500_000], 1_000_001),
            ([100_000, 200_000, 700_000], -999_999),
            ([0, 0], 7),
        ]
        for balances, total in cases:
            cats = [_cat(str(i), b) for i, b in enumerate(balances)]
            result = _build_subtransactions(total, cats)
            assert sum(s["amount"] for s in result) == total

    def test_category_ids_preserved_in_output(self):
        cats = [_cat("cat-x", 1_000_000), _cat("cat-y", 2_000_000)]
        result = _build_subtransactions(300_000, cats)
        assert result[0]["category_id"] == "cat-x"
        assert result[1]["category_id"] == "cat-y"
