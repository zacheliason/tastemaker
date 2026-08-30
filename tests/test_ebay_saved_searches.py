import json
from decimal import Decimal

from listing_agent import ebay


def test_saved_searches_parse_favorite_searches(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "client")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("EBAY_REFRESH_TOKEN", "refresh")

    class Response:
        text = '''<GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
          <FavoriteSearches><FavoriteSearch><SearchName>Clocks</SearchName><SearchQuery>mid century modern clocks</SearchQuery><PriceMax currencyID="USD">200</PriceMax><CategoryID>1</CategoryID></FavoriteSearch></FavoriteSearches>
        </GetMyeBayBuyingResponse>'''

        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "access"}

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("listing_agent.ebay.httpx.post", post)
    results = ebay.saved_searches()
    assert results[0]["query"] == "mid century modern clocks"
    assert "max_price_usd" not in results[0]
    assert calls[1][1]["headers"]["X-EBAY-API-IAF-TOKEN"] == "access"


def test_access_token_is_cached_until_expiry(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "client")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("EBAY_REFRESH_TOKEN", "refresh")
    monkeypatch.setattr(ebay, "_TOKEN_CACHE", None)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "access", "expires_in": 3600}

    calls = []
    monkeypatch.setattr(
        "listing_agent.ebay.httpx.post",
        lambda *args, **kwargs: (calls.append((args, kwargs)) or Response()),
    )

    assert ebay._access_token() == "access"
    assert ebay._access_token() == "access"
    assert len(calls) == 1


def test_fetch_paginates_deduplicates_and_ignores_bad_end_date(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "client")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")

    class Response:
        def __init__(self, items):
            self.items = items

        def raise_for_status(self):
            return None

        def json(self):
            return {"itemSummaries": self.items}

    monkeypatch.setattr(ebay, "_access_token", lambda: "access")
    calls = []

    def get(url, **kwargs):
        calls.append(kwargs["params"])
        if len(calls) == 1:
            return Response([
                {"itemId": "one", "title": "One", "price": {"value": "10", "currency": "USD"}, "itemEndDate": "bad"},
                {"itemId": "one", "title": "Duplicate"},
            ])
        return Response([{"itemId": "two", "title": "Two", "price": {"value": "20", "currency": "USD"}}])

    monkeypatch.setattr("listing_agent.ebay.httpx.get", get)
    results = ebay.fetch({"id": "search", "query": "clocks", "limit": 2})
    assert [item.external_id for item in results] == ["one", "two"]
    assert results[0].sale_end_at is None
    assert results[1].price is not None and results[1].price_usd == Decimal("20")
    assert calls == [{"q": "clocks", "limit": 2, "offset": 0}, {"q": "clocks", "limit": 1, "offset": 2}]


def test_fetch_extracts_keywords_from_saved_search_url(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "client")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    monkeypatch.setattr(ebay, "_access_token", lambda: "access")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"itemSummaries": []}

    calls = []

    def get(url, **kwargs):
        calls.append(kwargs["params"])
        return Response()

    monkeypatch.setattr("listing_agent.ebay.httpx.get", get)
    ebay.fetch({
        "id": "search",
        "query": "https://www.ebay.com/sch/i.html?_nkw=black+meermin+shoe+size+8.5&_dcat=53120&_geositeid=0&US%2520Shoe%2520Size=US%2520Men%25208%7CUS%2520Men%25208%252E5%7CUS%2520Men%25209%252E5&_udhi=200.00&_fcid=1&_sop=12&_stpos=22206-2974&_svsrch=1&_trksid=m194",
        "limit": 100,
    })

    assert calls == [{"q": "black meermin shoe size 8.5", "limit": 100, "offset": 0}]


def test_saved_searches_uses_account_values_over_defaults(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "client")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("EBAY_REFRESH_TOKEN", "refresh")

    class Response:
        text = '''<GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
          <FavoriteSearches><FavoriteSearch><SearchName>Clocks</SearchName><SearchQuery>mid century modern clocks</SearchQuery><PriceMax currencyID="USD">200</PriceMax></FavoriteSearch></FavoriteSearches>
        </GetMyeBayBuyingResponse>'''

        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "access"}

    def post(url, **kwargs):
        return Response()

    monkeypatch.setattr("listing_agent.ebay.httpx.post", post)
    results = ebay.saved_searches({"query": "configured query", "max_price_usd": 1})

    assert len(results) == 1
    assert results[0]["query"] == "mid century modern clocks"
    assert results[0]["max_price_usd"] == 1


def test_saved_searches_uses_result_defaults(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "client")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("EBAY_REFRESH_TOKEN", "refresh")

    class Response:
        text = '''<GetMyeBayBuyingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
          <FavoriteSearches><FavoriteSearch><SearchName>Clocks</SearchName><SearchQuery>clocks</SearchQuery></FavoriteSearch></FavoriteSearches>
        </GetMyeBayBuyingResponse>'''

        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "access"}

    monkeypatch.setattr("listing_agent.ebay.httpx.post", lambda *args, **kwargs: Response())
    result = ebay.saved_searches({"limit": 25, "sort": "bestMatch"})[0]

    assert result["limit"] == 25
    assert result["sort"] == "bestMatch"
