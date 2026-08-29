from __future__ import annotations

import httpx

from .config import required_env
from .urls import strip_query


def fetch_html(url: str) -> str:
    api_key = required_env("ZENROWS_API_KEY")["ZENROWS_API_KEY"]
    response = httpx.get(
        "https://api.zenrows.com/v1/",
        params={"apikey": api_key, "url": strip_query(url), "js_render": "true"},
        timeout=90,
    )
    response.raise_for_status()
    return response.text


async def fetch_lot_page(url: str, search_id: str):
    from .invaluable import parse_lot_page

    clean_url = strip_query(url)
    return parse_lot_page(fetch_html(clean_url), clean_url, search_id)
