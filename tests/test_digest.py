from datetime import datetime, timedelta, timezone

from listing_agent.digest import _price, _remaining, deliver, render


def test_price_only_shows_usd():
    assert _price("400.00", "EUR", "435.00") == "$435.00 USD"


def test_remaining_includes_sale_date():
    end = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)
    now = end - timedelta(days=18)
    assert _remaining(end, now) == "18 Days Remaining | Sale on September 16, 2026 at 10:00 AM"


def test_render_groups_listing_and_adds_feedback_links():
    text, markup = render([{
        "source": "ebay", "external_id": "abc", "title": "Cream trousers",
        "price": "80.00", "currency": "USD", "price_usd": "80.00",
        "url": "https://example.test/item", "image_urls": ["https://example.test/image.jpg"],
        "title_reason": "Matches size.", "taste_verdict": "like", "taste_reason": "Strong match."
    }], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc), "feedback@example.com")
    assert "Cream trousers" in text
    assert "Like: mailto:feedback@example.com" in text
    assert "Dislike" in markup
    assert "image.jpg" in markup


def test_render_empty_digest():
    text, markup = render([], "me@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))
    assert "No matching listings." in text
    assert "No matching listings." in markup


def test_empty_digest_is_not_delivered():
    class Connection:
        def execute(self, query, params):
            class Result:
                def fetchall(self):
                    return []
            return Result()

    assert deliver(Connection(), datetime(2026, 8, 28, tzinfo=timezone.utc), "digest@example.com") == 0
