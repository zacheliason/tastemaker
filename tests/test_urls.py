from listing_agent.urls import strip_query, strip_queries


def test_strip_query_removes_everything_after_question_mark():
    assert strip_query("https://example.test/item?a=1#fragment") == "https://example.test/item"
    assert strip_queries(["https://example.test/a?tracking=1", "", "https://example.test/b"]) == [
        "https://example.test/a", "https://example.test/b"
    ]
