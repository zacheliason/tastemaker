from datetime import datetime, timedelta, timezone

from listing_agent.digest import _price, _remaining, deliver, render


def test_price_only_shows_usd():
    assert _price("400.00", "EUR", "435.00") == "$435.00 USD"


def test_remaining_includes_sale_date():
    end = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)
    now = end - timedelta(days=18)
    assert _remaining(end, now) == "18 Days Remaining | Sale on September 16, 2026 at 10:00 AM"


def test_render_includes_llm_usage_footer():
    text, markup = render([], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc), usage={
        "prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15
    })
    assert "LLM usage: 12 prompt + 3 completion = 15 tokens" in text
    assert "LLM usage: 12 prompt + 3 completion = 15 tokens" in markup


def test_download_images_keeps_content_in_memory(monkeypatch):
    from listing_agent.digest import download_images

    class Response:
        headers = {"content-type": "image/jpeg"}
        content = b"jpeg-bytes"

        def raise_for_status(self):
            return None

    monkeypatch.setattr("listing_agent.digest.httpx.get", lambda *args, **kwargs: Response())
    sources, attachments = download_images([{"external_id": "abc", "image_urls": ["https://example.test/image.jpg"]}])
    assert sources["abc"].startswith("cid:listing-")
    assert attachments[0][1] == b"jpeg-bytes"


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


def test_render_marks_filtered_section():
    _, markup = render([{
        "section": "Filtered", "source": "invaluable", "external_id": "filtered-1", "title": "Expensive lot",
        "price": "1000.00", "currency": "USD", "price_usd": "1000.00", "url": "https://example.test/item",
        "image_urls": [], "filter_reason": "price exceeds limit", "taste_reason": None,
        "title_reason": None, "taste_verdict": "filtered"
    }], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))
    assert "FILTERED" in markup
    assert "border:1px solid #b85c52" in markup
    assert "price exceeds limit" in markup


def test_empty_digest_is_not_delivered():
    class Connection:
        def execute(self, query, params):
            class Result:
                def fetchall(self):
                    return []
            return Result()

    assert deliver(Connection(), datetime(2026, 8, 28, tzinfo=timezone.utc), "digest@example.com") == 0
