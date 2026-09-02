from email.message import EmailMessage
from datetime import timezone

from listing_agent.invaluable import _date, _listing_key, enrich_with_retry, parse_lot_page, parse_message
from listing_agent.models import Listing


def test_parse_message_extracts_listing_fields():
    message = EmailMessage()
    message.set_content('<a href="https://example.test/lot/123"><img src="https://example.test/image.jpg">Marc Chagall woodblock print</a><span>$250.00</span>', subtype="html")
    items = parse_message(message, {"id": "test", "include_keywords": ["Chagall"], "max_price": 300})
    assert len(items) == 1
    assert items[0].title == "Marc Chagall woodblock print"
    assert str(items[0].price) == "250.00"
    assert items[0].image_urls == ["https://example.test/image.jpg"]


def test_parse_message_excludes_configured_scraper_content():
    message = EmailMessage()
    message.set_content(
        '<a href="https://example.test/lot/123">Google cloud platform</a>', subtype="html"
    )
    assert parse_message(message, {"id": "test", "exclude_content": ["Google cloud platform"]}) == []


def test_content_exclusion_matches_enriched_title_without_matching_similar_text():
    from listing_agent.filters import content_exclusion

    assert content_exclusion(
        {"title": "ZenRows", "description": ""}, {"exclude_content": ["Zenrows"]}
    ) == "Zenrows"
    assert content_exclusion(
        {"title": "Google Cloud Platform", "description": ""},
        {"exclude_content": ["Google cloud platform"]},
    ) == "Google cloud platform"
    assert content_exclusion(
        {"title": "Google cloud platform print", "description": ""},
        {"exclude_content": ["Zenrows"]},
    ) is None
    assert content_exclusion(
        {"title": "Google cloud platform print", "description": ""},
        {"exclude_content": ["Google cloud platform"]},
    ) is None


def test_parse_message_extracts_recommendation_lot_links_and_deduplicates_artist():
    message = EmailMessage()
    message["Subject"] = "More art you may like"
    message.set_content(
        """<html><body>
        <h2>Lots Worth a Look</h2>
        <a href="https://www.invaluable.com/auction-lot/zdenek-sykora-c-abc123">
          Zdeněk Sýkora (1920 Louny 2011) Zdeněk Sýkora (1920 Louny 2011)
        </a>
        <a href="https://www.invaluable.com/discover-more">DISCOVER MORE</a>
        </body></html>""",
        subtype="html",
    )
    items = parse_message(message, {"id": "test", "include_keywords": ["Sýkora"]})
    assert len(items) == 1
    assert items[0].title == "Zdeněk Sýkora (1920 Louny 2011)"
    assert items[0].url == "https://www.invaluable.com/auction-lot/zdenek-sykora-c-abc123"
    assert items[0].image_urls == []


def test_parse_message_uses_recommendation_image_metadata_for_tracked_links():
    message = EmailMessage()
    message.set_content(
        """<a itemid="0022696213" href="https://invaluable.us-1.evergage.com/tecr?q=tracked">
          <img alt="Zdeněk Sýkora (1920 Louny 2011)"
               src="https://image.invaluable.com/lot.jpg">
        </a>""",
        subtype="html",
    )
    items = parse_message(message, {"id": "test"})
    assert len(items) == 1
    assert items[0].title == "Zdeněk Sýkora (1920 Louny 2011)"
    assert items[0].url == "https://www.invaluable.com/auction-lot/-0022696213"
    assert items[0].image_urls == ["https://image.invaluable.com/lot.jpg"]


def test_listing_key_matches_full_and_canonical_invaluable_slugs():
    email_url = "https://www.invaluable.com/auction-lot/Handsome-French-Napoleon-III-Ebony-Marble-Dial-344-c-5C250534A6"
    canonical_url = "https://www.invaluable.com/auction-lot/handsome-french-napoleon-iii-ebony-marble-dial-wa-344-c-5c250534a6"
    assert _listing_key(email_url) == _listing_key(canonical_url)


def test_date_parses_iso8601_and_normalizes_naive_values():
    assert _date("2026-09-16T10:00:00Z").tzinfo == timezone.utc
    assert _date("2026-09-16T10:00:00-04:00").utcoffset().total_seconds() == -4 * 3600
    assert _date("not-a-date") is None


def test_parse_lot_page_uses_product_json_ld():
    html = '''<html><head>
      <link rel="canonical" href="https://www.invaluable.com/auction-lot/example">
      <script type="application/ld+json">{"@type":"Product","name":"Josef Albers","description":"Screenprint on board.","image":"https://example.test/lot.jpg","sku":"E123","offers":{"price":2000,"priceCurrency":"EUR"}}</script>
    </head></html>'''
    item = parse_lot_page(html, "https://tracking.example/lot", "test")
    assert item.external_id == "E123"
    assert item.title == "Josef Albers"
    assert item.price == 2000
    assert item.currency == "EUR"
    assert item.url == "https://www.invaluable.com/auction-lot/example"


def test_parse_lot_page_reads_visible_auction_date():
    html = '''<html><body><div class="auction-date">September 16, 10:00 AM</div>
    <h1>Example lot</h1></body></html>'''
    item = parse_lot_page(html, "https://example.test/lot", "test")
    assert item.sale_end_at is not None
    assert item.sale_end_at.month == 9
    assert item.sale_end_at.day == 16
    assert item.sale_end_at.hour == 10


def test_zenrows_enrichment_fallback_keeps_retry_reasons(monkeypatch):
    async def fail(url, search_id):
        raise RuntimeError("ZenRows request failed")

    monkeypatch.setattr("listing_agent.zenrows.fetch_lot_page", fail)
    candidate = Listing("invaluable", "test", "id", "Lot", None, None, "https://example.test/lot")
    result = enrich_with_retry(candidate, attempts=2)
    assert result.raw_data["enrichment_status"] == "fallback_email"
    assert result.raw_data["enrichment_retry_errors"] == [
        {"attempt": 1, "error": "ZenRows request failed"},
        {"attempt": 2, "error": "ZenRows request failed"},
    ]
