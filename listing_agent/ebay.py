import httpx
from datetime import datetime

from .config import required_env
from .models import Listing
from .pricing import parse_price, to_usd


def fetch(search: dict) -> list[Listing]:
    env = required_env("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")
    marketplace = __import__("os").environ.get("EBAY_MARKETPLACE_ID", "EBAY_US")
    token = httpx.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        auth=(env["EBAY_CLIENT_ID"], env["EBAY_CLIENT_SECRET"]),
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    token.raise_for_status()
    response = httpx.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        params={"q": search["query"], "limit": min(search.get("limit", 100), 200)},
        headers={"Authorization": f"Bearer {token.json()['access_token']}", "X-EBAY-C-MARKETPLACE-ID": marketplace},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    listings = []
    for item in result.get("itemSummaries", []):
        price = item.get("price") or {}
        image = item.get("image", {}).get("imageUrl")
        amount, currency = parse_price(price.get("value"), price.get("currency"))
        end_at = item.get("itemEndDate") or item.get("endDate")
        sale_end_at = datetime.fromisoformat(end_at.replace("Z", "+00:00")) if end_at else None
        listings.append(Listing(
            source="ebay", search_id=search["id"], external_id=item["itemId"],
            title=item.get("title", ""), price=amount,
            currency=currency, price_usd=to_usd(amount, currency), url=item.get("itemWebUrl", ""),
            image_urls=[image] if image else [], description=item.get("shortDescription", ""),
            raw_data=item, sale_end_at=sale_end_at,
        ))
    return listings
