from __future__ import annotations

import logging
import httpx
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlsplit

from .config import required_env
from .models import Listing
from .pricing import parse_price, to_usd
from .urls import strip_query


logger = logging.getLogger(__name__)


def _keywords(value: str) -> str:
    """Convert an eBay saved-search URL to the Browse API keyword query."""
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    query = parse_qs(parsed.query).get("_nkw", [])
    return unquote(query[0]) if query else value


def _parse_end_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _access_token() -> str:
    env = required_env("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")
    refresh_token = __import__("os").environ.get("EBAY_REFRESH_TOKEN")
    if refresh_token:
        response = httpx.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            auth=(env["EBAY_CLIENT_ID"], env["EBAY_CLIENT_SECRET"]),
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    else:
        response = httpx.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            auth=(env["EBAY_CLIENT_ID"], env["EBAY_CLIENT_SECRET"]),
            data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        detail = response.text[:300].replace("\n", " ")
        raise RuntimeError(
            f"eBay OAuth failed with HTTP {response.status_code}: {detail}"
        ) from error
    return response.json()["access_token"]


def saved_searches(defaults: dict | None = None) -> list[dict]:
    """Read the authenticated buyer's Saved Searches from Trading API."""
    import os
    if not os.environ.get("EBAY_REFRESH_TOKEN"):
        raise RuntimeError("EBAY_REFRESH_TOKEN is required to read eBay account saved searches")
    token = _access_token()
    response = httpx.post(
        "https://api.ebay.com/ws/api.dll",
        headers={
            "X-EBAY-API-CALL-NAME": "GetMyeBayBuying",
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1255",
            "X-EBAY-API-SITEID": "0",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml",
        },
        content="""<?xml version="1.0" encoding="utf-8"?>
<GetMyeBayBuyingRequest xmlns="urn:ebay:apis:eBLBaseComponents"><DetailLevel>ReturnAll</DetailLevel><FavoriteSearches/></GetMyeBayBuyingRequest>""",
        timeout=60,
    )
    response.raise_for_status()
    root = ET.fromstring(response.text)
    if root.findtext(".//{*}Ack") == "Failure" or root.findall(".//{*}Errors/{*}Error"):
        raise RuntimeError("eBay saved-search request returned API errors")
    output = []
    for node in root.findall(".//{*}FavoriteSearch"):
        value = lambda name: node.findtext(f".//{{*}}{name}")
        query = value("SearchQuery") or value("QueryKeywords") or ""
        if not query:
            continue
        output.append({
            **(defaults or {}),
            "id": "ebay-saved-" + (value("SearchName") or query).lower().replace(" ", "-")[:80],
            "query": _keywords(query),
            "limit": 100,
            "category_ids": [value("CategoryID")] if value("CategoryID") else [],
            "exclude_keywords": [],
        })
    logger.info("eBay saved searches imported: %d", len(output))
    for search in output:
        logger.info(
            "eBay saved search: id=%s query=%r category=%s max_price_usd=%s",
            search["id"], search["query"], search.get("category"), search.get("max_price_usd"),
        )
    return output


def fetch(search: dict) -> list[Listing]:
    env = required_env("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")
    marketplace = __import__("os").environ.get("EBAY_MARKETPLACE_ID", "EBAY_US")
    token = _access_token()
    listings = []
    seen_ids = set()
    requested = max(1, min(int(search.get("limit", 100)), 10000))
    offset = 0
    while len(listings) < requested:
        page_limit = min(200, requested - len(listings))
        params = {"q": _keywords(search["query"]), "limit": page_limit, "offset": offset}
        category_ids = [str(value) for value in search.get("category_ids", []) if value]
        if category_ids:
            params["category_ids"] = ",".join(category_ids)
        response = httpx.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            params=params,
            headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": marketplace},
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
        items = result.get("itemSummaries") or []
        if not items:
            break
        for item in items:
            external_id = item.get("itemId")
            if not external_id or external_id in seen_ids:
                continue
            seen_ids.add(external_id)
            price = item.get("price") or {}
            image = strip_query(item.get("image", {}).get("imageUrl"))
            amount, currency = parse_price(price.get("value"), price.get("currency"))
            end_at = item.get("itemEndDate") or item.get("endDate")
            listings.append(Listing(
                source="ebay", search_id=search["id"], external_id=external_id,
                title=item.get("title", ""), price=amount,
                currency=currency, price_usd=to_usd(amount, currency), url=strip_query(item.get("itemWebUrl", "")),
                image_urls=[image] if image else [], description=item.get("shortDescription", ""),
                raw_data={**item, "_search_config": search}, sale_end_at=_parse_end_date(end_at),
            ))
            if len(listings) >= requested:
                break
        offset += len(items)
        if len(items) < page_limit:
            break
    return listings
