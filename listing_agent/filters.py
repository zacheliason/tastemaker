from __future__ import annotations

from datetime import datetime, timezone
import logging
import re
from .config import configured_sources, enabled_searches


logger = logging.getLogger(__name__)


def _normalized_content(value: str) -> str:
    return " ".join(value.casefold().split())


def content_exclusion(row: dict, search: dict) -> str | None:
    return next(
        (phrase for phrase in search.get("exclude_content", [])
         if _normalized_content(phrase) in {
             _normalized_content(row.get("title") or ""),
             _normalized_content(row.get("description") or ""),
         }),
        None,
    )


def _shoe_size_values(text: str) -> list[tuple[float, str | None]]:
    values = []
    pattern = re.compile(
        r"\b(?:(women(?:'s)?|womens|men(?:'s)?|mens|w)\s*)?"
        r"(?:shoe\s+size\s*)?(\d+(?:\.5)?)\b",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        gender = match.group(1)
        if not gender and not re.search(r"shoe\s+size", match.group(0), re.IGNORECASE):
            continue
        gender = "women" if gender and gender.casefold().startswith(("w", "women")) else "men" if gender else None
        values.append((float(match.group(2)), gender))
    return values


def _shoe_size_allowed(actual: object, allowed: list, gender: str | None = None) -> bool:
    try:
        actual_value = float(actual)
    except (TypeError, ValueError):
        return any(str(actual).casefold() == str(value).casefold() for value in allowed)
    if gender == "women":
        actual_value -= 1.5
    return any(actual_value == float(value) for value in allowed)


def _title_size_mismatch(row: dict, search: dict) -> tuple[str, str] | None:
    text = f"{row.get('title') or ''} {row.get('description') or ''}"
    for field, allowed in search.get("allowed_size_fields", {}).items():
        values = []
        if field == "waist":
            # Common trouser notation: the first number in 38x26 is the waist.
            values.extend(re.findall(r"\b(\d{2})\s*x\s*\d{2}\b", text, re.IGNORECASE))
            values.extend(re.findall(r"\b(?:waist|w)\s*(?:size\s*)?[:#-]?\s*(\d{2})\b", text, re.IGNORECASE))
        if field == "shoe_size":
            allowed = search.get("allowed_size_fields", {}).get(field, [])
            for value, gender in _shoe_size_values(text):
                if not _shoe_size_allowed(value, allowed, gender):
                    return field, str(value).removesuffix(".0")
        if values and not any(value.lower() == str(allowed_value).lower() for value in values for allowed_value in allowed):
            return field, values[0]
    return None


def evaluate(row: dict, search: dict) -> tuple[str, str | None]:
    excluded_content = content_exclusion(row, search)
    if excluded_content:
        return "filtered", f"excluded content: {excluded_content}"
    title = (row.get("title") or "").lower()
    description = (row.get("description") or "").lower()
    text = f"{title} {description}"
    excluded = [word.lower() for word in search.get("exclude_keywords", [])]
    found = next((word for word in excluded if word in text), None)
    if found:
        return "filtered", f"excluded keyword: {found}"

    title_size = _title_size_mismatch(row, search)
    if title_size:
        field, actual = title_size
        return "filtered", f"title size not allowed: {field}={actual}"

    max_price = search.get("max_price_usd")
    if max_price is not None:
        if row.get("price_usd") is None:
            return "filtered", "missing price_usd with maximum price configured"
        if row["price_usd"] > max_price:
            return "filtered", f"price_usd {row['price_usd']} exceeds max_price_usd {max_price}"

    for field, expected in search.get("required_size_fields", {}).items():
        actual = (row.get("size_fields") or {}).get(field)
        if actual is not None and str(actual).lower() != str(expected).lower():
            return "filtered", f"structured size mismatch: {field}={actual}"
    for field, allowed in search.get("allowed_size_fields", {}).items():
        actual = (row.get("size_fields") or {}).get(field)
        gender = (row.get("size_fields") or {}).get(f"{field}_gender")
        if actual is not None and (
            not _shoe_size_allowed(actual, allowed, gender) if field == "shoe_size"
            else not any(str(actual).lower() == str(value).lower() for value in allowed)
        ):
            return "filtered", f"structured size not allowed: {field}={actual} (allowed: {allowed})"
    return "passed", None


def apply(conn, searches: dict, source: str | None = None) -> dict[str, dict[str, int]]:
    configured = dict(configured_sources(searches))
    sources = [source] if source else list(configured)
    summary = {}
    for current_source in sources:
        if current_source not in configured:
            continue
        source_searches = {item["id"]: item for item in enabled_searches(configured[current_source])}
        rows = conn.execute("select id, search_id, title, description, price_usd, size_fields, raw_data from listings where source = %s", (current_source,)).fetchall()
        before = len(rows)
        passed = filtered = 0
        for row in rows:
            data = {"title": row[2], "description": row[3], "price_usd": row[4], "size_fields": row[5]}
            search = dict(source_searches.get(row[1]) or (row[6] or {}).get("_search_config", {}))
            search.setdefault("exclude_content", configured[current_source].get("exclude_content", []))
            if "max_price_usd" in configured[current_source]:
                search["max_price_usd"] = configured[current_source]["max_price_usd"]
            status, reason = evaluate(data, search)
            if status == "passed":
                passed += 1
            else:
                filtered += 1
            logger.info(
                "deterministic filter: source=%s search_id=%s listing_id=%s status=%s reason=%r",
                current_source, row[1], row[0], status, reason,
            )
            conn.execute("update listings set filter_status = %s, filter_reason = %s, filtered_at = %s where id = %s", (status, reason, datetime.now(timezone.utc) if reason else None, row[0]))
        summary[current_source] = {"before": before, "passed": passed, "filtered": filtered}
    return summary
