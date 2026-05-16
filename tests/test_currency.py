import pytest
from unittest.mock import MagicMock, patch

from ynab_reconciler.currency import convert_currency, parse_statement_input


class TestParseStatementInput:
    def test_plain_integer(self):
        assert parse_statement_input("1200") == (1200.0, None)

    def test_plain_float(self):
        assert parse_statement_input("1200.50") == (1200.50, None)

    def test_amount_with_currency(self):
        assert parse_statement_input("1000 EUR") == (1000.0, "EUR")

    def test_amount_with_currency_and_decimals(self):
        assert parse_statement_input("1234.56 USD") == (1234.56, "USD")

    def test_negative_with_currency(self):
        assert parse_statement_input("-500 USD") == (-500.0, "USD")

    def test_leading_trailing_whitespace(self):
        assert parse_statement_input("  1000 EUR  ") == (1000.0, "EUR")

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_statement_input("abc")

    def test_lowercase_currency_raises(self):
        with pytest.raises(ValueError):
            parse_statement_input("1000 eur")

    def test_two_letter_code_not_matched_as_currency(self):
        with pytest.raises(ValueError):
            parse_statement_input("1000 EU")

    @pytest.mark.parametrize(
        "raw",
        [
            "1 000 000,00",
            "1 000 000",
            "1,000,000.00",
            "1,000,000",
            "1.000.000,00",
            "1.000.000",
        ],
    )
    def test_one_million_formats(self, raw):
        assert parse_statement_input(raw) == (1_000_000.0, None)

    @pytest.mark.parametrize(
        "raw",
        [
            "1 000 000,00 EUR",
            "1 000 000 EUR",
            "1,000,000.00 EUR",
            "1,000,000 EUR",
        ],
    )
    def test_one_million_formats_with_currency(self, raw):
        assert parse_statement_input(raw) == (1_000_000.0, "EUR")

    def test_european_decimal_comma(self):
        assert parse_statement_input("1234,56") == (1234.56, None)

    def test_european_mixed(self):
        assert parse_statement_input("1.234,56") == (1234.56, None)

    def test_single_comma_three_digits_is_thousands(self):
        assert parse_statement_input("1,000") == (1000.0, None)

    def test_single_dot_three_digits_is_thousands(self):
        assert parse_statement_input("1.000") == (1000.0, None)

    def test_single_comma_non_three_digits_is_decimal(self):
        assert parse_statement_input("1,5") == (1.5, None)

    def test_negative_with_formatting(self):
        assert parse_statement_input("-1,000.50") == (-1000.5, None)

    def test_nbsp_thousands_separator(self):
        assert parse_statement_input("1 000") == (1000.0, None)


class TestConvertCurrency:
    def test_conversion_multiplies_by_rate(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"rate": 400.25}
        with patch("ynab_reconciler.currency.requests.get", return_value=mock_resp) as mock_get:
            result = convert_currency(100.0, "EUR", "HUF")
            mock_get.assert_called_once_with(
                "https://api.frankfurter.dev/v2/rate/EUR/HUF", timeout=10
            )
        assert abs(result - 40025.0) < 0.01

    def test_api_error_raises_value_error(self):
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 422
        mock_resp.text = "Unknown currency"
        with patch("ynab_reconciler.currency.requests.get", return_value=mock_resp):
            with pytest.raises(ValueError, match="conversion failed"):
                convert_currency(100.0, "XYZ", "HUF")

    def test_fractional_rate(self):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"rate": 0.003}
        with patch("ynab_reconciler.currency.requests.get", return_value=mock_resp):
            result = convert_currency(1000.0, "HUF", "EUR")
        assert abs(result - 3.0) < 0.0001
