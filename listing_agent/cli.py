import argparse
import asyncio
import json
import logging
from pathlib import Path
from datetime import datetime
from .config import adapter_for, configured_sources, enabled_searches, load_searches
from .db import save


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["feedback", "ingest", "filter", "import-references", "upload-references", "judge", "digest", "enrich-url"])
    parser.add_argument("--config", default="config/searches.json")
    parser.add_argument("--ai-config", default="config/ai.json")
    parser.add_argument("--digest-config", default="config/digest.json")
    parser.add_argument("--source", help="configured source name")
    parser.add_argument("--url")
    parser.add_argument("--search-id")
    parser.add_argument("--directory")
    parser.add_argument("--bucket", default="taste-references")
    parser.add_argument("--label", choices=["like", "dislike"], default="like", help="label for imported taste references")
    parser.add_argument("--to", dest="recipient")
    parser.add_argument("--since")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = load_searches(args.config)
    if args.command == "feedback":
        import os
        import psycopg
        from .feedback import ingest
        if not os.environ.get("DATABASE_URL"):
            parser.error("feedback requires DATABASE_URL")
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            count = ingest(conn, bucket=args.bucket)
        print(f"feedback references added: {count}")
        return
    if args.command == "enrich-url":
        if not args.url:
            parser.error("enrich-url requires --url")
        source_name = args.source or config.get("default_enrichment_source")
        if not source_name or source_name not in dict(configured_sources(config, enabled_only=False)):
            parser.error("enrich-url requires a configured --source")
        if source_name != "invaluable":
            parser.error(f"source adapter does not support URL enrichment: {source_name}")
        search_id = args.search_id or next((item["id"] for item in enabled_searches(config["sources"][source_name])), None)
        if not search_id:
            parser.error("enrich-url requires --search-id when the source has no enabled searches")
        from .zenrows import fetch_lot_page
        item = asyncio.run(fetch_lot_page(args.url, search_id))
        print(f"title: {item.title}")
        print(f"price: {item.price} {item.currency}")
        print(f"sale_end_at: {item.sale_end_at}")
        print(f"external_id: {item.external_id}")
        print(f"images: {len(item.image_urls)}")
        print(f"description: {item.description}")
        print(f"url: {item.url}")
        return
    if args.command == "filter":
        import os
        import psycopg
        from .filters import apply
        if not os.environ.get("DATABASE_URL"):
            parser.error("filter requires DATABASE_URL")
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            summary = apply(conn, config, args.source)
        for source, counts in summary.items():
            print(f"{source}: before={counts['before']} passed={counts['passed']} filtered={counts['filtered']}")
        return
    if args.command == "import-references":
        import os
        import psycopg
        from .references import import_directory
        if not args.directory:
            parser.error("import-references requires --directory")
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            count = import_directory(conn, args.directory, args.label)
        print(f"references imported: {count}")
        return
    if args.command == "upload-references":
        import os
        import psycopg
        from .references import upload_directory
        from .storage import SupabaseStorage
        if not args.directory:
            parser.error("upload-references requires --directory")
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            count = upload_directory(conn, args.directory, args.bucket, SupabaseStorage())
        print(f"references uploaded and reconciled: {count}")
        return
    if args.command == "judge":
        import os
        import psycopg
        from .ai import run_with_config
        if not os.environ.get("DATABASE_URL"):
            parser.error("judge requires DATABASE_URL")
        ai_config = json.loads(Path(args.ai_config).read_text())
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            count = run_with_config(conn, config, ai_config, args.source)
            print(f"AI judgments upserted: {count}")
        return
    if args.command == "digest":
        import os
        import psycopg
        from .digest import deliver, default_start
        digest_config = json.loads(Path(args.digest_config).read_text())
        recipient = args.recipient or os.environ.get("DIGEST_TO")
        if not recipient:
            parser.error("digest requires --to or DIGEST_TO")
        start = datetime.fromisoformat(args.since) if args.since else default_start()
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            count = deliver(conn, start, recipient, args.dry_run, digest_config.get("include_filtered", False))
        status = "rendered" if args.dry_run else ("sent" if count else "not sent; no listings")
        print(f"digest {status}: {count} listings")
        return
    configured = dict(configured_sources(config))
    sources = [args.source] if args.source else list(configured)
    total = 0
    for source in sources:
        settings = configured.get(source)
        if not settings:
            print(f"{source}: skipped; source is not configured or enabled")
            continue
        fetcher = adapter_for(settings).fetch
        source_total = 0
        adapter = adapter_for(settings)
        if settings.get("account_saved_searches"):
            defaults = dict(settings.get("saved_search_defaults") or {})
            if "max_price_usd" in settings:
                defaults["max_price_usd"] = settings["max_price_usd"]
            if "allowed_size_fields" in settings:
                defaults["allowed_size_fields"] = settings["allowed_size_fields"]
            if "exclude_content" in settings:
                defaults["exclude_content"] = settings["exclude_content"]
            account_searches = adapter.saved_searches(defaults)
            # Account mode is authoritative; configured searches are not a fallback.
            searches = account_searches
            logger.info("eBay effective saved searches: %d", len(searches))
            for search in searches:
                logger.info(
                    "eBay effective search: id=%s query=%r category=%s max_price_usd=%s",
                    search["id"], search["query"], search.get("category"), search.get("max_price_usd"),
                )
        else:
            searches = enabled_searches(settings)
        for search in searches:
            effective_search = {**(settings.get("saved_search_defaults") or {}), **search}
            if "allowed_size_fields" in settings:
                effective_search["allowed_size_fields"] = settings["allowed_size_fields"]
            if "exclude_content" in settings:
                effective_search["exclude_content"] = settings["exclude_content"]
            if settings.get("enrichment_provider"):
                effective_search["enrichment_provider"] = settings["enrichment_provider"]
            if "max_price_usd" in settings:
                effective_search["max_price_usd"] = settings["max_price_usd"]
            items = fetcher(effective_search)
            if not items:
                logger.info("%s search %s returned zero items; continuing", source, search["id"])
                continue
            for item in items:
                item.raw_data = {**item.raw_data, "_search_config": effective_search}
            source_total += save(items)
        print(f"{source}: fetched and upserted {source_total} listing records")
        total += source_total
    print(f"total: {total}")


if __name__ == "__main__":
    main()
