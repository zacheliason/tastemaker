from __future__ import annotations

import email
import hashlib
import imaplib
import json
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr

from bs4 import BeautifulSoup

from .config import required_env
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


def _date(value: str | None) -> datetime | None:
    if not value:
        return None


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


def parse_message(message: Message, search: dict) -> list[Listing]:
    soup = BeautifulSoup(_html(message), "html.parser")
    output = []
    images = soup.select('img[alt="lot image"][src]') or soup.select("a[href] img[src]")
    seen_urls = set()
    for image in images:
        link = image.find_parent("a", href=True)
        if not link:
            continue
        container = image.find_parent("table") or link.parent
        title_cell = container.select_one('td[style*="font-weight:bold"]')
        title = (
            title_cell.get_text(" ", strip=True)
            if title_cell
            else link.get_text(" ", strip=True)
        )
        if not title or title.lower() in {"lot image", "invaluable"}:
            continue
        image_urls = strip_queries([image["src"]])
        href = strip_query(
            link.get("title", "")
            if link.get("title", "").startswith("https://")
            else link["href"]
        )
        if href in seen_urls:
            continue
        seen_urls.add(href)
        block = container.get_text(" ", strip=True)
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


async def fetch_lot_page_browser(url: str, search_id: str) -> Listing:
    from playwright.async_api import async_playwright

    url = strip_query(url)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = await browser.new_page(
                viewport={"width": 1365, "height": 900},
                locale="en-US",
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            )
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=45_000
            )
            if response is None or response.status >= 500 or response.status == 404:
                status = response.status if response else "no response"
                raise RuntimeError(f"Invaluable browser request failed: HTTP {status}")
            # CloudFront may need several seconds to complete its JavaScript
            # check. Do not parse the challenge's placeholder as a real lot.
            last_title = ""
            for _ in range(12):
                listing = parse_lot_page(await page.content(), url, search_id)
                last_title = listing.title
                if listing.raw_data and listing.title.lower() not in {
                    "javascript is disabled",
                    "access denied",
                }:
                    return listing
                await page.wait_for_timeout(2_500)
            raise RuntimeError(
                f"Invaluable returned a browser challenge instead of lot data (title={last_title!r})"
            )
        finally:
            await browser.close()


def enrich_with_retry(
    candidate: Listing, attempts: int = 2, provider: str = "playwright"
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
            elif provider == "playwright":
                enriched = asyncio.run(
                    fetch_lot_page_browser(candidate.url, candidate.search_id)
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
        mailbox.select(os.environ.get("IMAP_FOLDER", "INBOX"), readonly=True)
        criteria = "ALL"
        if search.get("senders"):
            criteria = (
                "(" + " ".join(f'FROM "{sender}"' for sender in search["senders"]) + ")"
            )
        _, data = mailbox.search(None, criteria)
        listings = []
        for message_id in data[0].split():
            _, raw = mailbox.fetch(message_id, "(RFC822)")
            message = email.message_from_bytes(raw[0][1])
            sender = parseaddr(message.get("From", ""))[1].lower()
            if search.get("sender_domains") and not any(
                sender.endswith("@" + domain.lower())
                or ("@" + domain.lower() + ".") in sender
                for domain in search["sender_domains"]
            ):
                continue
            if search.get("subject_contains") and not any(
                term.lower() in _text(message.get("Subject")).lower()
                for term in search["subject_contains"]
            ):
                continue
            for candidate in parse_message(message, search):
                if _listing_key(candidate.url) in existing_urls:
                    candidate.raw_data["enrichment_status"] = "skipped_existing"
                    candidate.raw_data["enrichment_reason"] = "URL already exists in listings database"
                    listings.append(candidate)
                    continue
                enriched = enrich_with_retry(
                    candidate, provider=search.get("enrichment_provider", "playwright")
                )
                enriched.raw_data["email_subject"] = candidate.raw_data.get(
                    "email_subject", ""
                )
                amount, currency = parse_price(enriched.price, enriched.currency)
                enriched.price = amount
                enriched.currency = currency
                enriched.price_usd = to_usd(amount, currency)
                listings.append(enriched)
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
        return listings
    finally:
        try:
            mailbox.logout()
        except Exception:
            pass
