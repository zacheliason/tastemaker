from __future__ import annotations

import os
import re
from decimal import Decimal

import httpx


SYMBOL_CURRENCIES = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "C$": "CAD", "A$": "AUD"}


def parse_price(value: str | int | float | Decimal | None, currency: str | None = None) -> tuple[Decimal | None, str | None]:
    if value is None:
        return None, currency.upper() if currency else None
    if isinstance(value, Decimal):
        return value, currency.upper() if currency else None
    text = str(value).strip().upper()
    detected = currency.upper() if currency else None
    for symbol, code in sorted(SYMBOL_CURRENCIES.items(), key=lambda pair: -len(pair[0])):
        if symbol in text:
            detected = detected or code
            text = text.replace(symbol, "")
    code_match = re.search(r"\b([A-Z]{3})\b", text)
    if code_match:
        detected = detected or code_match.group(1)
        text = text.replace(code_match.group(1), "")
    number = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", text)
    if not number:
        return None, detected
    return Decimal(number.group(0).replace(",", "")), detected


def to_usd(amount: Decimal | None, currency: str | None) -> Decimal | None:
    if amount is None or not currency:
        return None
    currency = currency.upper()
    if currency == "USD":
        return amount
    api_key = os.environ.get("FREECURRENCYAPI_KEY")
    if not api_key:
        raise RuntimeError("Missing required environment variable: FREECURRENCYAPI_KEY")
    response = httpx.get(
        "https://api.freecurrencyapi.com/v1/latest",
        params={"apikey": api_key, "base_currency": currency, "currencies": "USD"},
        timeout=20,
    )
    response.raise_for_status()
    rate = Decimal(str(response.json()["data"]["USD"]))
    return (amount * rate).quantize(Decimal("0.01"))
