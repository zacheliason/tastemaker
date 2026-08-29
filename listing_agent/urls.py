from __future__ import annotations


def strip_query(value: str | None) -> str:
    """Remove tracking/query data from a URL before it enters stored data."""
    return (value or "").split("?", 1)[0]


def strip_queries(values: list[str]) -> list[str]:
    return [strip_query(value) for value in values if value]


def url_key(value: str | None) -> str:
    return strip_query(value).strip().lower().rstrip("/")
