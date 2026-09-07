from __future__ import annotations

import email
import hashlib
import imaplib
import json
import os
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr

from bs4 import BeautifulSoup
import httpx

from .config import required_env
from .filters import content_exclusion
from .models import Listing
from .pricing import parse_price, to_usd
from .urls import strip_queries, strip_query, url_key


def _text(value: str | None) -> str:
    if not value:
        return ""
    return "".join(
        part.decode(enc or "utf-8", errors="replace")
        if isinstance(part, bytes)
        else part
        for part, enc in decode_header(value)
    )


def _html(message: Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/html":
                return part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
    elif message.get_content_type() == "text/html":
        return message.get_payload(decode=True).decode(
            message.get_content_charset() or "utf-8", errors="replace"
        )
    return ""


def _price(text: str) -> tuple[Decimal | None, str | None]:
    match = re.search(r"([$€£])\s*([\d,]+(?:\.\d{1,2})?)", text)
    if not match:
        return None, None
    try:
        return Decimal(match.group(2).replace(",", "")), {
            "$": "USD",
            "€": "EUR",
            "£": "GBP",
        }[match.group(1)]
    except InvalidOperation:
        return None, None


def _listing_key(value: str | None) -> str:
    normalized = url_key(value)
    match = re.search(r"/auction-lot/.*-c-([a-z0-9]+)$", normalized)
    return f"invaluable-lot:{match.group(1)}" if match else normalized


def _move_message(mailbox, message_id: bytes, folder: str) -> None:
    """Move a message within the selected IMAP mailbox via COPY + delete."""
    if not folder:
        return
    status, _ = mailbox.create(folder)
    if status not in {"OK", "NO"}:
        raise RuntimeError(f"could not create IMAP folder {folder!r}")
    status, response = mailbox.copy(message_id, folder)
    if status != "OK":
        raise RuntimeError(
            f"could not copy message to IMAP folder {folder!r}: {response!r}"
        )
    status, response = mailbox.store(message_id, "+FLAGS", "(\\Deleted)")
    if status != "OK":
        raise RuntimeError(
            f"could not remove message from source IMAP folder: {response!r}"
        )


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _auction_date(value: str | None) -> datetime | None:
    if not value:
        return None
    match = re.search(r"([A-Za-z]+\s+\d{1,2},\s+\d{1,2}:\d{2}\s*[AP]M)", value, re.I)
    if not match:
        return None
    try:
        parsed = datetime.strptime(match.group(1), "%B %d, %I:%M %p").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    now = datetime.now(timezone.utc)
    parsed = parsed.replace(year=now.year)
    return parsed if parsed >= now else parsed.replace(year=now.year + 1)


def _deduplicate_repeated_text(value: str) -> str:
    """Recommendation emails repeat each artist for desktop and mobile cards."""
    words = value.split()
    midpoint = len(words) // 2
    if midpoint and len(words) % 2 == 0 and words[:midpoint] == words[midpoint:]:
        return " ".join(words[:midpoint])
    return value


def _catalog_links(message: Message) -> list[str]:
    soup = BeautifulSoup(_html(message), "html.parser")
    links = []
    for link in soup.select("a[href]"):
        text = link.get_text(" ", strip=True).lower()
        title = (link.get("title") or "").lower()
        if "view catalog" in text or "view new " in title and "catalog" in title:
            links.append(link["href"])
    return list(dict.fromkeys(links))


def _is_catalog_email(message: Message) -> bool:
    subject = _text(message.get("Subject")).lower()
    return "new auction" in subject and bool(_catalog_links(message))


def _resolve_catalog_url(url: str) -> str:
    if "/catalog/" in url:
        return url
    response = httpx.get(url, follow_redirects=True, timeout=30)
    response.raise_for_status()
    return str(response.url)


def _catalog_url(url: str, category: str, page: int) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.pop("utm_term", None)
    query.pop("utm_campaign", None)
    query.pop("utm_content", None)
    query.pop("utm_medium", None)
    query.pop("utm_source", None)
    query["page"] = str(page)
    query["size"] = "48"
    query["categoryName" if "jewelry" in category.lower() else "supercategoryName"] = category
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _catalog_available_categories(html: str, categories: list[str]) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    available = set()
    for checkbox in soup.select('input[type="checkbox"][aria-label]'):
        label = checkbox.get("aria-label", "").removesuffix(" checkbox")
        available.update(part.strip() for part in label.split(","))
    for label in soup.select("label"):
        text = label.get_text(" ", strip=True)
        available.update(
            category for category in categories
            if re.match(rf"^{re.escape(category)}(?:\d+)?$", text)
        )
    return [category for category in categories if category in available]


def parse_catalog_page(html: str, search: dict) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    output = []
    seen = set()
    for link in soup.select('a[href*="/auction-lot/"]'):
        url = strip_query(link.get("href"))
        if not url or url in seen:
            continue
        seen.add(url)
        title = link.get_text(" ", strip=True)
        image = link.select_one("img[src]")
        if not title:
            title = image.get("alt", "") if image else ""
        if not title:
            continue
        block = link.parent.get_text(" ", strip=True)
        price, currency = _price(block)
        output.append(
            Listing(
                "invaluable",
                search["id"],
                hashlib.sha256(url.encode()).hexdigest()[:32],
                title,
                price,
                currency,
                url,
                strip_queries([image["src"]]) if image else [],
                block,
                raw_data={"local": True, "email_subject": search.get("email_subject", "")},
            )
        )
    return output


def fetch_catalog_email(message: Message, search: dict) -> list[Listing]:
    from .zenrows import fetch_catalog_html

    categories = search.get("catalog_categories", [])
    output = []
    seen_catalogs = set()
    for tracked_url in _catalog_links(message):
        catalog_url = _resolve_catalog_url(tracked_url)
        catalog_key = strip_query(catalog_url)
        if catalog_key in seen_catalogs:
            continue
        seen_catalogs.add(catalog_key)
        first_page = fetch_catalog_html(catalog_url) if categories else ""
        available = _catalog_available_categories(first_page, categories)
        if not available:
            continue
        for category in available:
            page = 1
            while page <= search.get("catalog_max_pages", 100):
                page_html = fetch_catalog_html(_catalog_url(catalog_url, category, page))
                page_items = parse_catalog_page(
                    page_html,
                    {**search, "email_subject": _text(message.get("Subject"))},
                )
                if not page_items:
                    break
                output.extend(page_items)
                if len(page_items) < 48:
                    break
                page += 1
    unique = {}
    for item in output:
        unique[item.url] = item
    return list(unique.values())[: search.get("limit", 200)]


def parse_message(message: Message, search: dict) -> list[Listing]:
    soup = BeautifulSoup(_html(message), "html.parser")
    output = []
    images = soup.select('img[alt="lot image"][src]') or soup.select("a[href] img[src]")
    candidates = [(image.find_parent("a", href=True), image) for image in images]
    existing_hrefs = {link.get("href") for link, _ in candidates if link}
    candidates.extend(
        (link, link.select_one("img[src]"))
        for link in soup.select('a[href*="/auction-lot/"]')
        if link.get("href") not in existing_hrefs
    )
    seen_urls = set()
    for link, image in candidates:
        if not link:
            continue
        item_id = link.get("itemid") or (image.get("itemid") if image else None)
        if "/tecr" in link.get("href", "") and not item_id:
            continue
        container = (image.find_parent("table") if image else None) or link.parent
        title_cell = container.select_one('td[style*="font-weight:bold"]')
        title = (
            title_cell.get_text(" ", strip=True)
            if title_cell
            else _deduplicate_repeated_text(link.get_text(" ", strip=True))
            or _deduplicate_repeated_text(image.get("alt", "") if image else "")
        )
        if not title or title.lower() in {"lot image", "invaluable"}:
            continue
        image_urls = strip_queries([image["src"]]) if image else []
        href = (
            f"https://www.invaluable.com/auction-lot/-{item_id}"
            if item_id
            else strip_query(
                link.get("title", "")
                if link.get("title", "").startswith("https://")
                else link["href"]
            )
        )
        if href in seen_urls:
            continue
        seen_urls.add(href)
        block = container.get_text(" ", strip=True)
        excluded = content_exclusion({"title": title, "description": block}, search)
        if excluded:
            continue
        price, currency = _price(block)
        haystack = f"{title} {block}".lower()
        if search.get("include_keywords") and not any(
            k.lower() in haystack for k in search["include_keywords"]
        ):
            continue
        external_id = hashlib.sha256(href.encode()).hexdigest()[:32]
        output.append(
            Listing(
                "invaluable",
                search["id"],
                external_id,
                title,
                price,
                currency,
                href,
                image_urls,
                block,
                raw_data={"email_subject": _text(message.get("Subject"))},
            )
        )
    return output[: search.get("limit", 200)]


def parse_lot_page(html: str, fallback_url: str, search_id: str) -> Listing:
    soup = BeautifulSoup(html, "html.parser")
    product = {}
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            product = data
            break

    offers = product.get("offers") or {}
    images = product.get("image") or soup.select_one('meta[property="og:image"]')
    image_urls = (
        images
        if isinstance(images, list)
        else [images.get("content")]
        if hasattr(images, "get")
        else [images]
    )
    image_urls = strip_queries(image_urls)
    canonical = soup.select_one('link[rel="canonical"]')
    url = strip_query(
        canonical.get("href") if canonical else product.get("url") or fallback_url
    )
    title = product.get("name") or (
        soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else ""
    )
    description = product.get("description") or (
        soup.select_one('meta[property="og:description"]') or {}
    ).get("content", "")
    price = offers.get("price")
    auction_date = soup.select_one(".auction-date")
    sale_end_at = _date(
        product.get("auctionEndDate")
        or product.get("endDate")
        or offers.get("priceValidUntil")
    )
    sale_end_at = sale_end_at or _auction_date(
        auction_date.get_text(" ", strip=True) if auction_date else None
    )
    return Listing(
        source="invaluable",
        search_id=search_id,
        external_id=str(
            product.get("sku") or hashlib.sha256(url.encode()).hexdigest()[:32]
        ),
        title=title,
        price=Decimal(str(price)) if price is not None else None,
        currency=offers.get("priceCurrency"),
        url=url,
        image_urls=image_urls,
        description=description,
        raw_data=product,
        sale_end_at=sale_end_at,
    )


def enrich_with_retry(
    candidate: Listing, attempts: int = 2, provider: str = "zenrows"
) -> Listing:
    import asyncio

    errors = []
    for attempt in range(attempts):
        try:
            if provider == "zenrows":
                from .zenrows import fetch_lot_page

                enriched = asyncio.run(
                    fetch_lot_page(candidate.url, candidate.search_id)
                )
            else:
                raise RuntimeError(f"Unknown enrichment provider: {provider}")
            enriched.raw_data["enrichment_status"] = "success"
            enriched.raw_data["enrichment_attempts"] = attempt + 1
            if errors:
                enriched.raw_data["enrichment_retry_errors"] = errors
            return enriched
        except Exception as error:
            errors.append({"attempt": attempt + 1, "error": str(error)[:500]})
            if attempt == attempts - 1:
                candidate.raw_data["enrichment_status"] = "fallback_email"
                candidate.raw_data["enrichment_attempts"] = attempts
                candidate.raw_data["enrichment_error"] = errors[-1]["error"]
                candidate.raw_data["enrichment_retry_errors"] = errors
                return candidate
            time.sleep(2**attempt)


def fetch(search: dict) -> list[Listing]:
    env = required_env("IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD")
    import psycopg
    db_env = required_env("DATABASE_URL")
    with psycopg.connect(db_env["DATABASE_URL"]) as conn:
        existing_urls = {
            _listing_key(row[0])
            for row in conn.execute("select url from listings where source = 'invaluable'").fetchall()
        }
    mailbox = imaplib.IMAP4_SSL(
        env["IMAP_HOST"], int(os.environ.get("IMAP_PORT", "993"))
    )
    try:
        mailbox.login(env["IMAP_USERNAME"], env["IMAP_PASSWORD"])
        source_folder = os.environ.get("IMAP_FOLDER", "INBOX")
        ingested_folder = os.environ.get(
            "IMAP_INGESTED_FOLDER", "Invaluable/Ingested"
        )
        failed_folder = os.environ.get(
            "IMAP_FAILED_FOLDER", "Invaluable/Not Ingested"
        )
        mailbox.select(source_folder, readonly=False)
        criteria = "UNSEEN"
        if search.get("senders"):
            senders = search["senders"]
            sender_query = f'FROM "{senders[0]}"' if len(senders) == 1 else "(OR " + " ".join(f'FROM "{sender}"' for sender in senders) + ")"
            criteria = f"(UNSEEN {sender_query})"
        _, data = mailbox.search(None, criteria)
        listings = []
        moved_count = 0
        for message_id in data[0].split():
            _, raw = mailbox.fetch(message_id, "(RFC822)")
            destination = failed_folder
            try:
                message = email.message_from_bytes(raw[0][1])
                sender = parseaddr(message.get("From", ""))[1].lower()
                if search.get("sender_domains") and not any(
                    sender.endswith("@" + domain.lower())
                    or ("@" + domain.lower() + ".") in sender
                    for domain in search["sender_domains"]
                ):
                    candidates = []
                elif search.get("subject_contains") and not any(
                    term.lower() in _text(message.get("Subject")).lower()
                    for term in search["subject_contains"]
                ):
                    candidates = []
                else:
                    if _is_catalog_email(message):
                        candidates = fetch_catalog_email(message, search)
                    else:
                        candidates = parse_message(message, search)
                message_listings = []
                for candidate in candidates:
                    if _listing_key(candidate.url) in existing_urls:
                        candidate.raw_data["enrichment_status"] = "skipped_existing"
                        candidate.raw_data["enrichment_reason"] = "URL already exists in listings database"
                        message_listings.append(candidate)
                        continue
                    enriched = enrich_with_retry(
                        candidate, provider=search.get("enrichment_provider", "zenrows")
                    )
                    enriched.raw_data["email_subject"] = candidate.raw_data.get(
                        "email_subject", ""
                    )
                    if content_exclusion(
                        {"title": enriched.title, "description": enriched.description}, search
                    ):
                        continue
                    amount, currency = parse_price(enriched.price, enriched.currency)
                    enriched.price = amount
                    enriched.currency = currency
                    enriched.price_usd = to_usd(amount, currency)
                    message_listings.append(enriched)
                listings.extend(message_listings)
                if message_listings:
                    destination = ingested_folder
                mailbox.store(message_id, "+FLAGS", "(\\Seen)")
            except Exception as error:
                print(f"invaluable email processing failed: {error}")
            if destination != source_folder:
                _move_message(mailbox, message_id, destination)
            moved_count += 1
        statuses = {}
        for item in listings:
            status = item.raw_data.get("enrichment_status", "unknown")
            statuses[status] = statuses.get(status, 0) + 1
            print(
                f"invaluable listing: status={status} title={item.title!r} price={item.price} currency={item.currency} sale_end_at={item.sale_end_at}"
            )
            if status == "fallback_email":
                print(
                    f"invaluable enrichment failed: url={item.url} attempts={item.raw_data.get('enrichment_attempts')} errors={item.raw_data.get('enrichment_retry_errors')}"
                )
        print(f"invaluable enrichment: {statuses}")
        print(
            f"invaluable email folders: moved={moved_count} "
            f"ingested={ingested_folder!r} failed={failed_folder!r}"
        )
        return listings
    finally:
        try:
            mailbox.expunge()
        except Exception:
            pass
        try:
            mailbox.logout()
        except Exception:
            pass
