from __future__ import annotations

import html
import hashlib
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import quote

import httpx

from .config import required_env


MAX_INLINE_ATTACHMENTS = 500


def _feedback_link(address: str, action: str, source: str, external_id: str, title: str) -> str:
    subject = f"Listing feedback: {action}"
    body = f"action={action}\nsource={source}\nexternal_id={external_id}\ntitle={title}"
    # Keep the address readable in the mailto path; some mail clients do not
    # decode an escaped @ when populating the To field.
    return f"mailto:{address}?subject={quote(subject)}&body={quote(body)}"


def _price(original, currency, usd) -> str:
    if usd is not None:
        return f"${usd} USD"
    return "Price unavailable"


def _remaining(end_at, now: datetime | None = None) -> str:
    if not end_at:
        return ""
    now = now or datetime.now(timezone.utc)
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=timezone.utc)
    seconds = (end_at - now).total_seconds()
    if seconds <= 0:
        return f"Sale Ended | Sale on {_sale_date(end_at)}"
    days = int(seconds // 86400)
    remaining = f"{days} Day{'s' if days != 1 else ''} Remaining" if days else "Sale Ends Today"
    return f"{remaining} | Sale on {_sale_date(end_at)}"


def _sale_date(value: datetime) -> str:
    return value.strftime("%B %d, %Y at %I:%M %p").replace(" 0", " ")


def _pretty_date(value: datetime) -> str:
    return value.strftime("%B %d, %Y").replace(" 0", " ")


def _category_label(category: str | None) -> str:
    return (category or "Not assigned").replace("_", " ").title()


def render(rows: list[dict], recipient: str, start: datetime, feedback_recipient: str | None = None, usage: dict | None = None, image_sources: dict[str, str] | None = None) -> tuple[str, str]:
    feedback_recipient = feedback_recipient or recipient
    grouped = {}
    for row in rows:
        grouped.setdefault((row.get("section", "Passed"), row["source"]), []).append(row)
    subject = f"Daily listing digest: {len(rows)} match{'es' if len(rows) != 1 else ''}"
    text = [subject, f"Since {start.isoformat()}", ""]
    pretty_start = _pretty_date(start)
    blocks = [f'''<div style="margin:0;background:#f4f1eb;padding:32px 16px;color:#252321;font-family:Arial,Helvetica,sans-serif;line-height:1.5">
<div style="max-width:680px;margin:0 auto">
<p style="margin:0 0 8px;color:#8a8177;font-size:11px;letter-spacing:2px;text-transform:uppercase">Daily selection</p>
<h1 style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:30px;font-weight:400;letter-spacing:-.5px">{html.escape(subject)}</h1>
<p style="margin:8px 0 28px;color:#756d65;font-size:13px">Since {html.escape(pretty_start)}</p>''']
    for (section, source), items in sorted(grouped.items(), key=lambda item: (item[0][0] != "Passed", item[0][1])):
        text.extend([section.upper(), source.title(), "=" * len(source)])
        section_color = "#756d65" if section == "Passed" else "#9b5148"
        blocks.append(f'<h2 style="margin:28px 0 12px;padding-bottom:8px;border-bottom:1px solid #d8d1c7;font-size:12px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:{section_color}">{html.escape(section.upper())} · {html.escape(source.title())}</h2>')
        for row in items:
            reason = row.get("filter_reason") or row.get("taste_reason") or row.get("title_reason") or "Matched configured search"
            like = _feedback_link(feedback_recipient, "like", row["source"], row["external_id"], row["title"])
            dislike = _feedback_link(feedback_recipient, "dislike", row["source"], row["external_id"], row["title"])
            remaining = _remaining(row.get("sale_end_at"))
            category = _category_label(row.get("category"))
            classifier = f"{category} preference classifier" if row.get("category") else "None"
            text.extend([row["title"], _price(row["price"], row["currency"], row["price_usd"]), row["url"]])
            if remaining:
                text.append(remaining)
            text.extend([f"Category: {category}", f"Classifier used: {classifier}", f"Verdict: {row['taste_verdict']}. {reason}", f"Like: {like}", f"Dislike: {dislike}", ""])
            image = row["image_urls"][0] if row["image_urls"] else ""
            image_source = (image_sources or {}).get(row["external_id"], image)
            image_html = f'<img src="{html.escape(image_source, quote=True)}" alt="Listing image" width="320" style="display:block;width:100%;max-width:320px;height:auto;max-height:240px;object-fit:contain"><br>' if image_source else ""
            filtered = section == "Filtered"
            card_border = "#b85c52" if filtered else "#ddd6cc"
            filtered_label = '<p style="margin:0 0 10px;color:#a14d45;font-size:10px;font-weight:700;letter-spacing:1.5px">FILTERED</p>' if filtered else ""
            blocks.append(f'''<div style="margin:0 0 16px;padding:18px;background:#fffefa;border:1px solid {card_border};border-radius:8px;box-shadow:0 2px 8px rgba(50,40,30,.04)">
 {image_html}{filtered_label}<h3 style="margin:12px 0 6px;font-family:Georgia,'Times New Roman',serif;font-size:20px;font-weight:400;line-height:1.25"><a style="color:#252321;text-decoration:none" href="{html.escape(row['url'], quote=True)}">{html.escape(row['title'])}</a></h3>
 <p style="margin:0 0 4px;font-size:13px;color:#514b45">{html.escape(_price(row['price'], row['currency'], row['price_usd']))}</p>
 {f'<p style="margin:0 0 10px;font-size:12px;color:#8a8177;letter-spacing:.2px">{html.escape(remaining)}</p>' if remaining else ''}
 <p style="margin:0 0 10px;font-size:12px;color:#756d65">Category: <strong>{html.escape(category)}</strong><br>Classifier used: <strong>{html.escape(classifier)}</strong></p>
 <p style="margin:0 0 16px;font-size:14px;color:#514b45">{html.escape(reason)}</p>
<p style="margin:0;font-size:13px"><a style="display:inline-block;padding:7px 12px;border:1px solid #b9aa98;border-radius:4px;color:#514b45;text-decoration:none" href="{html.escape(like, quote=True)}">Like</a>&nbsp;&nbsp;<a style="display:inline-block;padding:7px 12px;border:1px solid #b9aa98;border-radius:4px;color:#514b45;text-decoration:none" href="{html.escape(dislike, quote=True)}">Dislike</a></p>
</div>''')
    if not rows:
        text.append("No matching listings.")
        blocks.append("<p style=\"padding:18px;background:#fffefa;border:1px solid #ddd6cc;border-radius:8px\">No matching listings.</p>")
    usage = usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage_line = f"LLM usage: {usage['prompt_tokens']} prompt + {usage['completion_tokens']} completion = {usage['total_tokens']} tokens"
    text.extend(["", usage_line])
    blocks.append(f'<p style="margin:28px 0 0;padding-top:12px;border-top:1px solid #d8d1c7;color:#8a8177;font-size:11px">{html.escape(usage_line)}</p>')
    blocks.append("</div></div>")
    return "\n".join(text), "".join(blocks)


def fetch_rows(conn, start: datetime, include_filtered: bool = False) -> list[dict]:
    rows = conn.execute("""select l.source, l.external_id, l.title, l.price, l.currency, l.price_usd,
        l.url, l.image_urls, l.sale_end_at, l.filter_status, l.filter_reason,
        j.title_reason, j.title_pass, j.category, j.taste_verdict, j.taste_reason
        from listings l left join ai_judgments j on j.listing_id = l.id
        where l.fetched_at >= %s and ((l.filter_status = 'passed' and j.title_pass = true and j.taste_verdict in ('like', 'uncertain'))
          or (%s and l.filter_status = 'filtered'))
        order by l.filter_status, l.source, l.fetched_at desc""", (start, include_filtered)).fetchall()
    keys = ("source", "external_id", "title", "price", "currency", "price_usd", "url", "image_urls", "sale_end_at", "filter_status", "filter_reason", "title_reason", "title_pass", "category", "taste_verdict", "taste_reason")
    output = [dict(zip(keys, row)) for row in rows]
    for row in output:
        row["section"] = "Passed" if row["filter_status"] == "passed" else "Filtered"
        if row["section"] == "Filtered":
            row["taste_verdict"] = "filtered"
    return output


def fetch_usage(conn, start: datetime) -> dict:
    row = conn.execute("select coalesce(sum(prompt_tokens), 0), coalesce(sum(completion_tokens), 0), coalesce(sum(total_tokens), 0) from llm_usage where recorded_at >= %s", (start,)).fetchone()
    return {"prompt_tokens": row[0], "completion_tokens": row[1], "total_tokens": row[2]}


def download_images(rows: list[dict]) -> tuple[dict[str, str], list[tuple[str, bytes, str, str]]]:
    """Fetch images only in memory for this message; do not retain marketplace images."""
    sources, attachments = {}, []
    for row in rows:
        if len(attachments) >= MAX_INLINE_ATTACHMENTS:
            break
        url = (row.get("image_urls") or [None])[0]
        if not url or not url.lower().startswith("https://"):
            continue
        try:
            response = httpx.get(url, timeout=20, follow_redirects=True)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if not content_type.startswith("image/") or len(response.content) > 5 * 1024 * 1024:
                continue
            maintype, subtype = content_type.split("/", 1)
            cid = f"listing-{hashlib.sha256(row['external_id'].encode()).hexdigest()[:16]}@digest"
            sources[row["external_id"]] = f"cid:{cid}"
            attachments.append((cid, response.content, maintype, subtype))
        except (httpx.HTTPError, ValueError):
            continue
    return sources, attachments


def send(message: EmailMessage, host: str, port: int, username: str, password: str) -> None:
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)


def deliver(conn, start: datetime, recipient: str, dry_run: bool = False, include_filtered: bool = False) -> int:
    rows = fetch_rows(conn, start, include_filtered)
    passed_count = sum(row["section"] == "Passed" for row in rows)
    if not passed_count and not dry_run:
        return 0
    feedback_recipient = os.environ.get("IMAP_USERNAME") or recipient
    image_sources, attachments = download_images(rows) if not dry_run else ({}, [])
    text, markup = render(rows, recipient, start, feedback_recipient, fetch_usage(conn, start), image_sources)
    message = EmailMessage()
    message["Subject"] = f"Daily listing digest: {len(rows)} match{'es' if len(rows) != 1 else ''}"
    message["From"] = os.environ.get("DIGEST_FROM") or os.environ.get("SMTP_USERNAME") or os.environ.get("IMAP_USERNAME", "listing-agent@localhost")
    message["To"] = recipient
    message.set_content(text)
    message.add_alternative(markup, subtype="html")
    if attachments:
        html_part = message.get_payload()[-1]
        for cid, data, maintype, subtype in attachments:
            html_part.add_related(data, maintype=maintype, subtype=subtype, cid=cid, disposition="inline")
    if not dry_run:
        host = os.environ.get("SMTP_HOST") or ("smtp.gmail.com" if os.environ.get("IMAP_HOST") == "imap.gmail.com" else "")
        username = os.environ.get("SMTP_USERNAME") or os.environ.get("IMAP_USERNAME", "")
        password = os.environ.get("SMTP_PASSWORD") or os.environ.get("IMAP_PASSWORD", "")
        missing = [name for name, value in (("SMTP_HOST", host), ("SMTP_USERNAME/IMAP_USERNAME", username), ("SMTP_PASSWORD/IMAP_PASSWORD", password)) if not value]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        send(message, host, int(os.environ.get("SMTP_PORT", "587")), username, password)
        conn.execute("insert into digest_runs (digest_date, recipient, item_count) values (%s,%s,%s) on conflict (digest_date, recipient) do update set item_count=excluded.item_count, sent_at=now()", (start.date(), recipient, len(rows)))
    return len(rows)


def default_start() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=1)
