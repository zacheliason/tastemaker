from listing_agent.urls import strip_query, strip_queries, url_key
from listing_agent.filters import evaluate


def test_strip_query_removes_everything_after_question_mark():
    assert strip_query("https://example.test/item?a=1#fragment") == "https://example.test/item"
    assert strip_queries(["https://example.test/a?tracking=1", "", "https://example.test/b"]) == [
        "https://example.test/a", "https://example.test/b"
    ]


def test_allowed_size_fields_rejects_out_of_range_structured_sizes():
    search = {"allowed_size_fields": {"waist": [28, 29, 30], "shoe_size": [8, 8.5]}}
    assert evaluate({"title": "trousers", "size_fields": {"waist": 38}}, search) == (
        "filtered", "structured size not allowed: waist=38 (allowed: [28, 29, 30])"
    )
    assert evaluate({"title": "trousers", "size_fields": {"waist": 30}}, search) == ("passed", None)
    assert evaluate({"title": "shoes", "size_fields": {"shoe_size": 8.5}}, search) == ("passed", None)
    assert evaluate({"title": "shoes", "size_fields": {"shoe_size": 9.5, "shoe_size_gender": "women"}}, search) == ("passed", None)
    assert evaluate({"title": "women's shoe size 10", "size_fields": {}}, search) == ("passed", None)
    assert evaluate({"title": "women's shoe size 11", "size_fields": {}}, search) == (
        "filtered", "title size not allowed: shoe_size=11"
    )
    assert evaluate({"title": "Polo trousers Mens 38x26", "size_fields": {}}, search) == (
        "filtered", "title size not allowed: waist=38"
    )
    assert url_key(" HTTPS://EXAMPLE.TEST/item/?tracking=1 ") == "https://example.test/item"
