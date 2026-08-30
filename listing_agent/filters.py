from __future__ import annotations

from datetime import datetime, timezone
from .config import configured_sources, enabled_searches


def evaluate(row: dict, search: dict) -> tuple[str, str | None]:
    title = (row.get("title") or "").lower()
    description = (row.get("description") or "").lower()
    text = f"{title} {description}"
    excluded = [word.lower() for word in search.get("exclude_keywords", [])]
    found = next((word for word in excluded if word in text), None)
    if found:
        return "filtered", f"excluded keyword: {found}"

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
            if "max_price_usd" in configured[current_source]:
                search["max_price_usd"] = configured[current_source]["max_price_usd"]
            status, reason = evaluate(data, search)
            if status == "passed":
                passed += 1
            else:
                filtered += 1
            conn.execute("update listings set filter_status = %s, filter_reason = %s, filtered_at = %s where id = %s", (status, reason, datetime.now(timezone.utc) if reason else None, row[0]))
        summary[current_source] = {"before": before, "passed": passed, "filtered": filtered}
    return summary
