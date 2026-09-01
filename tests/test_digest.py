from datetime import datetime, timedelta, timezone

from listing_agent.digest import _price, _remaining, deliver, render
from listing_agent.translation import translate_rows


def test_price_only_shows_usd():
    assert _price("400.00", "EUR", "435.00") == "$435.00 USD"


def test_remaining_includes_sale_date():
    end = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)
    now = end - timedelta(days=18)
    assert _remaining(end, now) == "18 Days Remaining | Sale on September 16, 2026 at 10:00 AM"


def test_render_includes_llm_cost_footer():
    text, markup = render([], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc), usage={
        "prompt_tokens": 12, "completion_tokens": 3, "cache_read_tokens": 5
    })
    expected = "Estimated LLM cost: $0.000005 (input $0.000001, output $0.000004, cache read $0.000000)"
    assert expected in text
    assert expected in markup
    assert "tokens" not in text


def test_render_includes_listing_description_and_new_title():
    text, markup = render([{
        "source": "ebay", "external_id": "abc", "title": "Cream trousers",
        "price": "80.00", "currency": "USD", "price_usd": "80.00",
        "url": "https://example.test/item", "image_urls": [],
        "description": "Soft wool\nwith a relaxed cut.", "taste_verdict": "like",
    }], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))
    assert text.startswith("Tastemaker Digest: 1 matches")
    assert "Description: Soft wool with a relaxed cut." in text
    assert "<strong>Description</strong><br>Soft wool with a relaxed cut." in markup
    assert "Tastemaker Digest: 1 matches" in markup
    assert "background:#edf4f8" in markup
    assert "background:#ababab" not in markup


def test_translate_rows_batches_and_labels_cached_result(monkeypatch):
    class Result:
        def fetchall(self):
            return []

        def fetchone(self):
            return (0,)

    class Connection:
        def __init__(self):
            self.inserts = []

        def execute(self, query, params):
            if query.startswith("insert into description_translations"):
                self.inserts.append(params)
            return Result()

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"translations": [{"detectedSourceLanguage": "de", "translatedText": "A beautiful chair"}]}}

    monkeypatch.setenv("GOOGLE_TRANSLATE_API_KEY", "test-key")
    monkeypatch.setattr("listing_agent.translation.httpx.post", lambda *args, **kwargs: Response())
    rows = [{"section": "Passed", "description": "Ein schoener Stuhl"}]
    conn = Connection()
    usage = {}

    assert translate_rows(conn, rows, usage) == 1
    assert rows[0]["description"] == "(TRANSLATED FROM DE: A beautiful chair)"
    assert len(conn.inserts) == 1
    assert usage == {"characters": len("Ein schoener Stuhl")}


def test_render_reports_translation_usage():
    text, markup = render([], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc), translation_usage={"characters": 1234})
    assert "Google Translation usage: 1,234 source characters this run" in text
    assert "Google Translation usage: 1,234 source characters this run" in markup


def test_render_preserves_safe_description_html_without_allowing_scripts():
    _, markup = render([{
        "source": "invaluable", "external_id": "html-1", "title": "Pecos Valley",
        "price": "20.00", "currency": "USD", "price_usd": "20.00",
        "url": "https://example.test/item", "image_urls": [],
        "description": "<b>Gustave Baumann</b><br>German American<script>alert('x')</script>",
        "taste_verdict": "like",
    }], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))

    assert "<strong>Description</strong><br><b>Gustave Baumann</b><br/>German American" in markup
    assert "&lt;b&gt;Gustave Baumann&lt;/b&gt;" not in markup
    assert "<script" not in markup


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


def test_download_images_caps_inline_attachments(monkeypatch):
    from listing_agent.digest import MAX_INLINE_ATTACHMENTS, download_images

    class Response:
        headers = {"content-type": "image/jpeg"}
        content = b"jpeg-bytes"

        def raise_for_status(self):
            return None

    calls = 0

    def get_image(*args, **kwargs):
        nonlocal calls
        calls += 1
        return Response()

    monkeypatch.setattr("listing_agent.digest.httpx.get", get_image)
    rows = [
        {"external_id": str(index), "image_urls": [f"https://example.test/{index}.jpg"]}
        for index in range(MAX_INLINE_ATTACHMENTS + 1)
    ]

    sources, attachments = download_images(rows)

    assert len(attachments) == MAX_INLINE_ATTACHMENTS
    assert len(sources) == MAX_INLINE_ATTACHMENTS
    assert calls == MAX_INLINE_ATTACHMENTS


def test_render_groups_listing_and_adds_feedback_links():
    text, markup = render([{
        "source": "ebay", "external_id": "abc", "title": "Cream trousers",
        "price": "80.00", "currency": "USD", "price_usd": "80.00",
        "url": "https://example.test/item", "image_urls": ["https://example.test/image.jpg"],
        "title_reason": "Matches size.", "category": "home_decor", "taste_verdict": "like", "taste_reason": "Strong match."
    }], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc), "feedback@example.com")
    assert "Cream trousers" in text
    assert "Like: mailto:feedback@example.com" in text
    assert "Dislike" in markup
    assert "image.jpg" in markup
    assert "Category: Home Decor" in text
    assert "Classifier used: <strong>Home Decor preference classifier</strong>" in markup


def test_render_shows_when_no_classifier_was_used():
    _, markup = render([{
        "source": "invaluable", "external_id": "fast-1", "title": "Fast tracked lot",
        "price": "50.00", "currency": "USD", "price_usd": "50.00", "url": "https://example.test/item",
        "image_urls": [], "taste_verdict": "like", "category": None,
    }], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))
    assert "Category: <strong>Not Assigned</strong>" in markup
    assert "Classifier used: <strong>None</strong>" in markup


def test_render_empty_digest():
    text, markup = render([], "me@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))
    assert "No matching listings." in text
    assert "No matching listings." in markup


def test_render_marks_filtered_section():
    _, markup = render([{
        "section": "Filtered", "source": "invaluable", "external_id": "filtered-1", "title": "Expensive lot",
        "price": "1000.00", "currency": "USD", "price_usd": "1000.00", "url": "https://example.test/item",
        "image_urls": [], "description": "Private seller notes", "filter_reason": "price exceeds limit", "taste_reason": None,
        "title_reason": None, "taste_verdict": "filtered"
    }], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))
    assert "FILTERED" in markup
    assert "background:#ffffff;border:1px solid #c77983" in markup
    assert "price exceeds limit" in markup
    assert "Private seller notes" not in markup

    text, _ = render([{
        "section": "Filtered", "source": "invaluable", "external_id": "filtered-1", "title": "Expensive lot",
        "price": "1000.00", "currency": "USD", "price_usd": "1000.00", "url": "https://example.test/item",
        "image_urls": [], "description": "Private seller notes", "filter_reason": "price exceeds limit",
        "taste_verdict": "filtered"
    }], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))
    assert "Private seller notes" not in text
    assert "Description:" not in text


def test_render_places_all_passed_listings_before_filtered_listings():
    rows = [
        {
            "section": "Filtered", "source": "ebay", "external_id": "filtered-1", "title": "Filtered listing",
            "price": "1000.00", "currency": "USD", "price_usd": "1000.00", "url": "https://example.test/filtered",
            "image_urls": [], "filter_reason": "price exceeds limit", "taste_verdict": "filtered",
        },
        {
            "section": "Passed", "source": "invaluable", "external_id": "passed-1", "title": "Passed invaluable listing",
            "price": "50.00", "currency": "USD", "price_usd": "50.00", "url": "https://example.test/passed-1",
            "image_urls": [], "taste_verdict": "like",
        },
        {
            "section": "Passed", "source": "ebay", "external_id": "passed-2", "title": "Passed ebay listing",
            "price": "60.00", "currency": "USD", "price_usd": "60.00", "url": "https://example.test/passed-2",
            "image_urls": [], "taste_verdict": "uncertain",
        },
    ]

    _, markup = render(rows, "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))

    assert markup.index("Passed ebay listing") < markup.index("Filtered listing")
    assert markup.index("Passed invaluable listing") < markup.index("Filtered listing")


def test_render_sorts_listings_by_usd_within_each_source():
    rows = [
        {
            "source": "ebay", "external_id": "expensive", "title": "Expensive",
            "price": "100.00", "currency": "USD", "price_usd": "100.00",
            "url": "https://example.test/expensive", "image_urls": [], "taste_verdict": "like",
        },
        {
            "source": "ebay", "external_id": "cheap", "title": "Cheap",
            "price": "20.00", "currency": "USD", "price_usd": "20.00",
            "url": "https://example.test/cheap", "image_urls": [], "taste_verdict": "like",
        },
        {
            "source": "invaluable", "external_id": "other", "title": "Other source",
            "price": "1.00", "currency": "USD", "price_usd": "1.00",
            "url": "https://example.test/other", "image_urls": [], "taste_verdict": "like",
        },
    ]

    _, markup = render(rows, "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))

    assert markup.index("Cheap") < markup.index("Expensive")


def test_empty_digest_is_not_delivered():
    class Connection:
        def execute(self, query, params):
            class Result:
                def fetchall(self):
                    return []
            return Result()

    assert deliver(Connection(), datetime(2026, 8, 28, tzinfo=timezone.utc), "digest@example.com") == 0


def test_digest_is_not_sent_twice_for_same_date(monkeypatch):
    class Result:
        def fetchall(self):
            return []

        def fetchone(self):
            return (1,)

    class Connection:
        def execute(self, query, params):
            if query.startswith("select 1 from digest_runs"):
                return Result()
            return Result()

    monkeypatch.setattr("listing_agent.digest.send", lambda *args: (_ for _ in ()).throw(AssertionError("sent twice")))
    assert deliver(Connection(), datetime(2026, 8, 28, tzinfo=timezone.utc), "digest@example.com") == 0
