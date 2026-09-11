from datetime import datetime, timezone

from listing_agent.filters import apply


def test_apply_scopes_rows_to_since_window():
    since = datetime(2026, 9, 9, 20, 36, tzinfo=timezone.utc)

    class Result:
        def fetchall(self):
            return [(1, "search-1", "Listing", "", 50, {}, {})]

    class Connection:
        def __init__(self):
            self.select_query = None
            self.select_params = None

        def execute(self, query, params=()):
            if query.startswith("select "):
                self.select_query = query
                self.select_params = params
                return Result()
            return Result()

    conn = Connection()
    summary = apply(conn, {
        "sources": {
            "ebay": {
                "enabled": True,
                "searches": [{"id": "search-1", "enabled": True}],
            },
        },
    }, since=since)

    assert "fetched_at >= %s" in conn.select_query
    assert "digest_seen_at is null" in conn.select_query
    assert conn.select_params == ("ebay", since)
    assert summary == {"ebay": {"before": 1, "passed": 1, "filtered": 0}}
