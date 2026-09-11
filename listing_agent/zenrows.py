from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from .config import required_env
from .urls import strip_query


def _fetch_html(url: str, clean_url: bool = True) -> str:
    if not urlsplit(url).scheme or not urlsplit(url).netloc:
        raise ValueError(f"ZenRows requires an absolute target URL, got {url!r}")
    api_key = required_env("ZENROWS_API_KEY")["ZENROWS_API_KEY"]
    response = httpx.get(
        "https://api.zenrows.com/v1/",
        params={"apikey": api_key, "url": strip_query(url) if clean_url else url, "js_render": "true"},
        timeout=90,
        follow_redirects=True,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        detail = response.text.strip().replace("\n", " ")[:500]
        raise RuntimeError(
            f"ZenRows request failed with HTTP {response.status_code}: {detail}"
        ) from error
    return response.text


def fetch_html(url: str) -> str:
    return _fetch_html(url)


def fetch_catalog_html(url: str) -> str:
    return _fetch_html(url, clean_url=False)


async def fetch_lot_page(url: str, search_id: str):
    from .invaluable import parse_lot_page

    clean_url = strip_query(url)
    return parse_lot_page(fetch_html(clean_url), clean_url, search_id)
