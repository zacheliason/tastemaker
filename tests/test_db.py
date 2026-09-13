import sys
import types

from listing_agent.db import clear_stale, save
from listing_agent.models import Listing


def test_save_skips_previously_seen_ebay_item(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://example")

    class Cursor:
        def __init__(self):
            self.queries = []
            self.rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, params=None):
            self.queries.append(query)
            return self

        def fetchone(self):
            return (1,)

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def cursor(self):
            return self.cursor_instance

    connection = Connection()
    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=lambda _: connection))

    item = Listing(
        "ebay", "search", "v1|123", "A listing", None, None,
        "https://www.ebay.com/itm/123?tracking=ignored",
    )

    assert save([item]) == 0
    assert len(connection.cursor_instance.queries) == 1
    assert "select 1 from listings" in connection.cursor_instance.queries[0]


def test_clear_stale_deletes_finished_and_undated_old_listings():
    class Connection:
        def __init__(self):
            self.query = None

        def execute(self, query):
            self.query = query
            return types.SimpleNamespace(rowcount=3)

    connection = Connection()

    assert clear_stale(connection) == 3
    assert "sale_end_at < now()" in connection.query
    assert "sale_end_at is null" in connection.query
    assert "interval '2 months'" in connection.query
