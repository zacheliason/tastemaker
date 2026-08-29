import json
import os
from .models import Listing
from .urls import strip_queries, strip_query


def save(listings: list[Listing]) -> int:
    import psycopg
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("Missing required environment variable: DATABASE_URL")
    rows = 0
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            for item in listings:
                item.url = strip_query(item.url)
                item.image_urls = strip_queries(item.image_urls)
                existing = cur.execute(
                    "select external_id from listings where source = %s and lower(url) = lower(%s) and external_id <> %s",
                    (item.source, item.url, item.external_id),
                ).fetchone()
                external_id = existing[0] if existing else item.external_id
                cur.execute("""
                    insert into listings (source, search_id, external_id, title, price, currency, price_usd, url, image_urls, description, size_fields, raw_data, sale_end_at, fetched_at)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s::jsonb,%s,%s)
                    on conflict (source, external_id) do update set
                      title = excluded.title,
                      price = excluded.price,
                      currency = excluded.currency,
                      price_usd = excluded.price_usd,
                      url = excluded.url,
                      image_urls = excluded.image_urls,
                       description = excluded.description,
                       sale_end_at = excluded.sale_end_at,
                       raw_data = excluded.raw_data,
                      fetched_at = excluded.fetched_at
                """, (item.source, item.search_id, external_id, item.title, item.price, item.currency, item.price_usd, item.url, json.dumps(item.image_urls), item.description, json.dumps(item.size_fields), json.dumps(item.raw_data), item.sale_end_at, item.fetched_at))
                rows += cur.rowcount
    return rows
