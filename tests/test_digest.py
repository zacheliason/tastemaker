from datetime import datetime, timedelta, timezone

from io import BytesIO

from PIL import Image

from listing_agent.digest import (
    _normalize_attachment_image,
    _price,
    _remaining,
    _source_color,
    deliver,
    fetch_efficiency,
    fetch_rows,
    outcomes_figure,
    pipeline_outcomes,
    render,
)
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


def test_fetch_efficiency_uses_listing_fetch_date_not_judgment_date():
    class Result:
        def fetchall(self):
            return [(datetime(2026, 8, 28).date(), 14, 3, 7, 3)]

    class Connection:
        def __init__(self):
            self.query = None
            self.params = None

        def execute(self, query, params):
            self.query = query
            self.params = params
            return Result()

    conn = Connection()
    start = datetime(2026, 8, 28, 15, 53, tzinfo=timezone.utc)
    efficiency = fetch_efficiency(conn, start, include_filtered=True)

    assert efficiency == [{
        "date": datetime(2026, 8, 28).date(),
        "like": 14,
        "dislike": 3,
        "discrete_filter_failures": 7,
        "taste_classifier_failures": 3,
    }]
    assert "l.fetched_at >= greatest(day, %s)" in conn.query
    assert "l.filter_status = 'passed'" in conn.query
    assert "j.title_pass = true" in conn.query
    assert "j.taste_verdict in ('like', 'uncertain')" in conn.query
    assert "count(distinct l.id)" in conn.query
    assert "judged_at::date" not in conn.query
    assert conn.params == (True, start.date(), start.date(), start)


def test_pipeline_outcomes_aggregate_digest_window_semantics():
    efficiency = [
        {"like": 4, "discrete_filter_failures": 2, "taste_classifier_failures": 1},
        {"like": 6, "discrete_filter_failures": 3, "taste_classifier_failures": 2},
    ]

    assert pipeline_outcomes(efficiency) == {
        "passed": 10,
        "discrete_filter_failures": 5,
        "taste_classifier_failures": 3,
    }


def test_pipeline_outcomes_chart_is_normalized_and_rendered_at_end_of_pulse():
    efficiency = [{"date": datetime(2026, 8, 28).date(), "like": 4, "dislike": 1, "discrete_filter_failures": 2, "taste_classifier_failures": 1}]
    normalized = _normalize_attachment_image(outcomes_figure(efficiency))
    assert normalized is not None
    with Image.open(BytesIO(normalized)) as image:
        assert image.format == "JPEG"

    _, markup = render(
        [], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc),
        efficiency=efficiency,
        efficiency_image_source="cid:efficiency-pulse",
        outcomes_image_source="cid:pipeline-outcomes",
    )
    pulse_image = markup.index('src="cid:efficiency-pulse"')
    outcomes_image = markup.index('src="cid:pipeline-outcomes"')
    pulse_block_end = markup.rindex('</td></tr></table>', pulse_image, outcomes_image)
    assert outcomes_image > pulse_block_end
    assert 'width="100%"' in markup[pulse_image - 250:pulse_image]
    assert 'clear:both' in markup[pulse_image - 250:pulse_image]
    assert 'width="100%"' in markup[outcomes_image - 250:outcomes_image]
    assert 'clear:both' in markup[outcomes_image - 250:outcomes_image]
    assert "Failed by taste classifier (logistic regression) 1" in "".join(render(
        [], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc), efficiency=efficiency
    )[0])


def test_source_colors_are_stable_and_not_limited_to_eight_buckets():
    sources = [f"source-{index}" for index in range(20)]
    colors = [_source_color(source) for source in sources]

    assert colors == [_source_color(source) for source in sources]
    assert len(set(colors)) == len(colors)


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
    assert "Daily edit" not in markup
    assert "A considered edit" not in markup
    assert "Curated with a point of view" not in markup
    assert ">T</div>" not in markup


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
        output = BytesIO()
        Image.new("RGB", (2400, 1200), "red").save(output, format="JPEG")
        content = output.getvalue()

        def raise_for_status(self):
            return None

    monkeypatch.setattr("listing_agent.digest.httpx.get", lambda *args, **kwargs: Response())
    sources, attachments = download_images([{"external_id": "abc", "image_urls": ["https://example.test/image.jpg"]}])
    assert sources["abc"].startswith("cid:listing-")
    assert attachments[0][2:] == ("image", "jpeg")
    with Image.open(BytesIO(attachments[0][1])) as image:
        assert image.size == (1600, 800)


def test_download_images_skips_invalid_image(monkeypatch):
    from listing_agent.digest import download_images

    class Response:
        headers = {"content-type": "image/jpeg"}
        content = b"not-an-image"

        def raise_for_status(self):
            return None

    monkeypatch.setattr("listing_agent.digest.httpx.get", lambda *args, **kwargs: Response())
    sources, attachments = download_images([{"external_id": "abc", "image_urls": ["https://example.test/image.jpg"]}])
    assert sources == {}
    assert attachments == []


def test_download_images_caps_inline_attachments(monkeypatch):
    from listing_agent.digest import MAX_INLINE_ATTACHMENTS, download_images

    class Response:
        headers = {"content-type": "image/jpeg"}
        output = BytesIO()
        Image.new("RGB", (1, 1), "red").save(output, format="JPEG")
        content = output.getvalue()

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


def test_deliver_attaches_normalized_pulse_images_as_inline_jpegs(monkeypatch):
    import listing_agent.digest as digest

    class Result:
        def fetchone(self):
            return None

    class Connection:
        def __init__(self):
            self.queries = []

        def execute(self, query, params):
            self.queries.append((query, params))
            return Result()

    listing_image = BytesIO()
    Image.new("RGB", (1, 1), "red").save(listing_image, format="JPEG")
    efficiency = [{"date": datetime(2026, 8, 28).date(), "like": 2, "dislike": 1, "discrete_filter_failures": 3, "taste_classifier_failures": 1}]
    sent = []
    monkeypatch.setattr(digest, "fetch_rows", lambda *args: [{
        "source": "ebay", "external_id": "listing-1", "title": "A listing", "price": "1", "currency": "USD",
        "price_usd": "1", "url": "https://example.test/item", "image_urls": ["https://example.test/image.jpg"],
        "taste_verdict": "like",
    }])
    monkeypatch.setattr(digest, "download_images", lambda rows: ({"listing-1": "cid:listing-1@digest"}, [("listing-1@digest", listing_image.getvalue(), "image", "jpeg")]))
    monkeypatch.setattr(digest, "translate_rows", lambda *args: None)
    monkeypatch.setattr(digest, "fetch_efficiency", lambda *args: efficiency)
    monkeypatch.setattr(digest, "fetch_usage", lambda *args: {"prompt_tokens": 0, "completion_tokens": 0, "cache_read_tokens": 0})
    monkeypatch.setattr(digest, "send", lambda message, *args: sent.append(message))
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_USERNAME", "user")
    monkeypatch.setenv("SMTP_PASSWORD", "pass")

    connection = Connection()
    assert digest.deliver(connection, datetime(2026, 8, 28, tzinfo=timezone.utc), "digest@example.com") == 1
    assert any(
        query.startswith("update listings set digest_seen_at") and params == ("ebay", "listing-1")
        for query, params in connection.queries
    )
    related = [part for part in sent[0].walk() if part.get_content_maintype() == "image"]
    assert {part["Content-ID"] for part in related} == {"listing-1@digest", "efficiency-pulse", "pipeline-outcomes"}
    assert all(part.get_content_subtype() == "jpeg" for part in related)
    assert all(part.get_content_disposition() == "inline" for part in related)


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
    assert "Classifier used" not in text
    assert "Classifier used" not in markup


def test_render_uses_a_discrete_category_badge():
    _, markup = render([{
        "source": "ebay", "external_id": "category-1", "title": "A chair",
        "price": "80.00", "currency": "USD", "price_usd": "80.00",
        "url": "https://example.test/item", "image_urls": [],
        "category": "home_decor", "taste_verdict": "like",
    }], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))
    assert ">HOME DECOR</span>" in markup
    assert "background:#dcecf0;border:1px solid #dcecf0;color:#35636b" in markup
    assert "Category: <strong>Home Decor</strong>" not in markup


def test_render_does_not_show_classifier_metadata():
    _, markup = render([{
        "source": "invaluable", "external_id": "fast-1", "title": "Fast tracked lot",
        "price": "50.00", "currency": "USD", "price_usd": "50.00", "url": "https://example.test/item",
        "image_urls": [], "taste_verdict": "like", "category": None,
    }], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))
    assert "Category: <strong>Not Assigned</strong>" in markup
    assert "Classifier used" not in markup


def test_render_empty_digest():
    text, markup = render([], "me@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))
    assert "No matching listings." in text
    assert "No matching listings." in markup


def test_render_marks_taste_filtered_section():
    _, markup = render([{
        "section": "Filtered", "source": "invaluable", "external_id": "filtered-1", "title": "Expensive lot",
        "price": "1000.00", "currency": "USD", "price_usd": "1000.00", "url": "https://example.test/item",
        "image_urls": [], "description": "Private seller notes", "filter_reason": "price exceeds limit", "taste_reason": "Not a taste match",
        "title_reason": None, "taste_verdict": "dislike"
    }], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))
    assert ">filtered</span>" in markup
    assert ">FILTERED</p>" not in markup
    assert "DISLIKE / EDIT VERDICT" not in markup
    assert "background:#ffffff;border:1px solid #c77983" in markup
    assert "Not a taste match" in markup
    assert "price exceeds limit" not in markup
    assert "Private seller notes" not in markup

    text, _ = render([{
        "section": "Filtered", "source": "invaluable", "external_id": "filtered-1", "title": "Expensive lot",
        "price": "1000.00", "currency": "USD", "price_usd": "1000.00", "url": "https://example.test/item",
        "image_urls": [], "description": "Private seller notes", "filter_reason": "price exceeds limit", "taste_reason": "Not a taste match",
        "taste_verdict": "dislike"
    }], "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))
    assert "Private seller notes" not in text
    assert "Description:" not in text


def test_render_uses_status_and_stable_distinct_source_badges():
    rows = [
        {
            "source": "ebay", "external_id": "passed", "title": "Passed listing",
            "price": "10.00", "currency": "USD", "price_usd": "10.00",
            "url": "https://example.test/passed", "image_urls": [], "taste_verdict": "like",
        },
        {
            "source": "invaluable", "external_id": "filtered", "title": "Filtered listing",
            "price": "20.00", "currency": "USD", "price_usd": "20.00",
            "url": "https://example.test/filtered", "image_urls": [], "taste_verdict": "dislike",
            "section": "Filtered", "taste_reason": "Not a taste match",
        },
    ]

    _, markup = render(rows, "digest@example.com", datetime(2026, 8, 28, tzinfo=timezone.utc))

    assert ">Passed</span>" in markup
    assert ">filtered</span>" in markup
    assert "LIKE / EDIT VERDICT" not in markup
    assert "DISLIKE / EDIT VERDICT" not in markup
    assert _source_color("ebay") == _source_color("ebay")
    assert _source_color("ebay") != _source_color("invaluable")
    assert f"background:{_source_color('ebay')};border:1px solid {_source_color('ebay')}" in markup
    assert f"background:{_source_color('invaluable')};border:1px solid {_source_color('invaluable')}" in markup


def test_render_places_all_passed_listings_before_filtered_listings():
    rows = [
        {
            "section": "Filtered", "source": "ebay", "external_id": "filtered-1", "title": "Filtered listing",
            "price": "1000.00", "currency": "USD", "price_usd": "1000.00", "url": "https://example.test/filtered",
            "image_urls": [], "filter_reason": "price exceeds limit", "taste_reason": "Not a taste match", "taste_verdict": "dislike",
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


def test_fetch_rows_only_includes_taste_filtered_items():
    class Result:
        def fetchall(self):
            return [(
                "ebay", "disliked", "Disliked listing", "10.00", "USD", "10.00", None,
                "https://example.test/disliked", [], None, {}, "passed", None, "Relevant", True,
                "art", "dislike", "Not a taste match",
            )]

    class Connection:
        def execute(self, query, params):
            assert "l.filter_status = 'passed'" in query
            assert "l.digest_seen_at is null" in query
            assert "j.taste_verdict = 'dislike'" in query
            assert params[0] is True
            return Result()

    rows = fetch_rows(Connection(), datetime(2026, 8, 28, tzinfo=timezone.utc), include_filtered=True)

    assert rows[0]["section"] == "Filtered"
    assert rows[0]["taste_verdict"] == "dislike"


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
