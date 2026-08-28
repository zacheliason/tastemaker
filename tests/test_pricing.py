from decimal import Decimal

from listing_agent.pricing import parse_price, to_usd


def test_parse_price_accepts_symbol_and_code():
    assert parse_price("$1,234.50 USD") == (Decimal("1234.50"), "USD")
    assert parse_price("EUR 2000") == (Decimal("2000"), "EUR")


def test_usd_does_not_require_exchange_api():
    assert to_usd(Decimal("12.34"), "USD") == Decimal("12.34")
