from email.message import EmailMessage
from listing_agent.invaluable import parse_lot_page, parse_message


def test_parse_message_extracts_listing_fields():
    message = EmailMessage()
    message.set_content('<a href="https://example.test/lot/123"><img src="https://example.test/image.jpg">Marc Chagall woodblock print</a><span>$250.00</span>', subtype="html")
    items = parse_message(message, {"id": "test", "include_keywords": ["Chagall"], "max_price": 300})
    assert len(items) == 1
    assert items[0].title == "Marc Chagall woodblock print"
    assert str(items[0].price) == "250.00"
    assert items[0].image_urls == ["https://example.test/image.jpg"]


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
