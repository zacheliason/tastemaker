from __future__ import annotations

import html
import hashlib
import os
import smtplib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup, Comment

from .config import required_env
from .translation import translate_rows


MAX_INLINE_ATTACHMENTS = 500
MAX_IMAGE_BYTES = 5 * 1024 * 1024
INPUT_COST_PER_MILLION = 0.20
OUTPUT_COST_PER_MILLION = 1.20
CACHE_READ_COST_PER_MILLION = 0.02


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


def _description(value: str | None) -> str:
    return " ".join((value or "").split())


def _description_html(value: str | None) -> str:
    """Keep useful description markup while removing executable HTML."""
    soup = BeautifulSoup(value or "", "html.parser")
    allowed = {"b", "strong", "i", "em", "u", "br", "p", "ul", "ol", "li"}
    for node in soup.find_all(string=lambda text: isinstance(text, Comment)):
        node.extract()
    for tag in soup.find_all(True):
        if tag.name in {"script", "style", "iframe", "object", "embed", "form"}:
            tag.decompose()
        elif tag.name not in allowed:
            tag.unwrap()
        else:
            tag.attrs = {}
    return " ".join(soup.decode_contents().split())


def _usd_sort_key(row: dict) -> tuple[int, Decimal]:
    value = row.get("price_usd")
    if value is None:
        return (1, Decimal("0"))
    try:
        return (0, Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return (1, Decimal("0"))


def _usage_cost(usage: dict) -> tuple[float, float, float, float]:
    cache_read_tokens = usage.get("cache_read_tokens", 0)
    input_tokens = max(0, usage["prompt_tokens"] - cache_read_tokens)
    input_cost = input_tokens * INPUT_COST_PER_MILLION / 1_000_000
    output_cost = usage["completion_tokens"] * OUTPUT_COST_PER_MILLION / 1_000_000
    cache_read_cost = cache_read_tokens * CACHE_READ_COST_PER_MILLION / 1_000_000
    return input_cost, output_cost, cache_read_cost, input_cost + output_cost + cache_read_cost


def render(rows: list[dict], recipient: str, start: datetime, feedback_recipient: str | None = None, usage: dict | None = None, image_sources: dict[str, str] | None = None, translation_usage: dict | None = None) -> tuple[str, str]:
    feedback_recipient = feedback_recipient or recipient
    grouped = {}
    for row in rows:
        grouped.setdefault((row.get("section", "Passed"), row["source"]), []).append(row)
    subject = f"Tastemaker Digest: {len(rows)} matches"
    passed_count = sum(row.get("section", "Passed") == "Passed" for row in rows)
    filtered_count = len(rows) - passed_count
    source_count = len({row["source"] for row in rows})
    text = [subject, f"Since {start.isoformat()}", ""]
    pretty_start = _pretty_date(start)
    summary = f"{passed_count} selected · {source_count} source{'s' if source_count != 1 else ''}"
    if filtered_count:
        summary += f" · {filtered_count} filtered"
    text.insert(2, summary)
    blocks = [f'''<div style="margin:0;background:#edf4f8;padding:24px 12px 44px;color:#182b2b;font-family:Arial,Helvetica,sans-serif;line-height:1.5">
<style>
  @media only screen and (max-width:600px) {{
    .digest-wrap {{ width:100% !important; }}
    .listing-media, .listing-copy {{ display:block !important; width:100% !important; box-sizing:border-box !important; }}
    .listing-media {{ padding:0 !important; }}
    .listing-media img {{ max-width:100% !important; max-height:none !important; }}
    .listing-copy {{ padding:22px 18px 20px !important; }}
    .digest-title {{ font-size:32px !important; }}
    .masthead-cell {{ padding:28px 22px 24px !important; }}
    .summary-cell, .section-cell {{ padding-left:18px !important; padding-right:18px !important; }}
  }}
</style>
<div class="digest-wrap" style="max-width:720px;margin:0 auto">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse:collapse;background:#182b2b">
  <tr><td class="masthead-cell" style="padding:30px 34px 27px;border-bottom:5px solid #d7ed62">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"><tr>
      <td><p style="margin:0 0 20px;color:#d7ed62;font-size:10px;font-weight:700;letter-spacing:2.6px;text-transform:uppercase">Tastemaker <span style="color:#71827c">/</span> Daily edit</p>
      <h1 class="digest-title" style="margin:0;color:#f7f5ee;font-family:Georgia,'Times New Roman',serif;font-size:38px;line-height:1.02;font-weight:400;letter-spacing:-1px">{html.escape(subject)}</h1>
      <p style="margin:15px 0 0;color:#b9c7c0;font-size:12px;letter-spacing:.25px">A considered edit from {html.escape(pretty_start)}</p></td>
      <td width="54" valign="top" style="padding-top:2px;text-align:right"><div style="width:42px;height:42px;border:1px solid #536761;border-radius:50%;color:#d7ed62;font-family:Georgia,serif;font-size:19px;line-height:42px;text-align:center">T</div></td>
    </tr></table>
  </td></tr>
</table>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse:collapse;background:#f7f5ee;border-left:1px solid #dce6e1;border-right:1px solid #dce6e1">
  <tr><td class="summary-cell" style="padding:18px 34px;border-bottom:1px solid #dce6e1"><p style="margin:0;color:#55706b;font-size:10px;font-weight:700;letter-spacing:1.6px;text-transform:uppercase">{html.escape(summary)}</p><p style="margin:5px 0 0;color:#9aa8a2;font-family:Georgia,serif;font-size:14px">Curated with a point of view.</p></td></tr>
</table>''']
    for (section, source), items in sorted(grouped.items(), key=lambda item: (item[0][0] != "Passed", item[0][1])):
        text.extend([section.upper(), source.title(), "=" * len(source)])
        section_color = "#557c1d" if section == "Passed" else "#a6534c"
        section_note = "Selected for your edit" if section == "Passed" else "Set aside by the configured filters"
        blocks.append(f'''<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse:collapse;background:#f7f5ee;border-left:1px solid #dce6e1;border-right:1px solid #dce6e1">
 <tr><td class="section-cell" style="padding:32px 34px 13px"><p style="margin:0 0 8px;color:#a6b1ab;font-size:10px;font-weight:700;letter-spacing:2px">SOURCE / {html.escape(source.title()).upper()}</p><h2 style="margin:0 0 4px;color:{section_color};font-family:Georgia,'Times New Roman',serif;font-size:25px;line-height:1.1;font-weight:400;letter-spacing:-.3px">{html.escape(section)}</h2><p style="margin:0;color:#7b8984;font-size:12px">{html.escape(section_note)}</p></td></tr></table>''')
        for row in sorted(items, key=_usd_sort_key):
            filtered = section == "Filtered"
            reason = row.get("filter_reason") or row.get("taste_reason") or row.get("title_reason") or "Matched configured search"
            like = _feedback_link(feedback_recipient, "like", row["source"], row["external_id"], row["title"])
            dislike = _feedback_link(feedback_recipient, "dislike", row["source"], row["external_id"], row["title"])
            remaining = _remaining(row.get("sale_end_at"))
            category = _category_label(row.get("category"))
            classifier = f"{category} preference classifier" if row.get("category") else "None"
            description = "" if filtered else _description(row.get("description"))
            text.extend([row["title"], _price(row["price"], row["currency"], row["price_usd"]), row["url"]])
            if remaining:
                text.append(remaining)
            text.extend([f"Category: {category}", f"Classifier used: {classifier}", f"Verdict: {row['taste_verdict']}. {reason}", f"Like: {like}", f"Dislike: {dislike}", ""])
            if description:
                text.insert(-1, f"Description: {description}")
            image = row["image_urls"][0] if row["image_urls"] else ""
            image_source = (image_sources or {}).get(row["external_id"], image)
            image_html = f'<img src="{html.escape(image_source, quote=True)}" alt="Listing image" width="270" style="display:block;width:100%;max-width:270px;height:auto;max-height:250px;object-fit:cover">' if image_source else '<div style="height:110px;background:#e8eeea;color:#8ca09a;font-size:10px;letter-spacing:1.2px;text-align:center;text-transform:uppercase;line-height:110px">No image supplied</div>'
            card_background = "#ffffff"
            card_border = "#c77983" if filtered else "#cbdbe5"
            filtered_label = '<p style="margin:0 0 10px;color:#a14d58;font-size:10px;font-weight:700;letter-spacing:1.5px">FILTERED</p>' if filtered else ""
            description_markup = "" if filtered else _description_html(row.get("description"))
            description_html = f'<p style="margin:0 0 12px;font-size:13px;line-height:1.45;color:#55706b"><strong>Description</strong><br>{description_markup}</p>' if description_markup else ""
            verdict = row.get("taste_verdict", "uncertain").title()
            blocks.append(f'''<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse:collapse;background:#f7f5ee;border-left:1px solid #dce6e1;border-right:1px solid #dce6e1">
 <tr><td style="padding:8px 34px 20px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse:collapse;background:{card_background};border:1px solid {card_border}">
 <tr><td class="listing-media" width="42%" valign="top" style="padding:0;background:#e8eeea">{image_html}</td><td class="listing-copy" width="58%" valign="top" style="padding:23px 25px 21px">
   {filtered_label}<p style="margin:0 0 9px;color:{section_color};font-size:10px;font-weight:700;letter-spacing:1.7px;text-transform:uppercase">{verdict} / EDIT VERDICT</p><h3 style="margin:0 0 10px;font-family:Georgia,'Times New Roman',serif;font-size:23px;line-height:1.1;font-weight:400;letter-spacing:-.35px"><a style="color:#182b2b;text-decoration:none" href="{html.escape(row['url'], quote=True)}">{html.escape(row['title'])}</a></h3>
   <p style="margin:0 0 5px;color:#557c1d;font-size:17px;font-weight:700;letter-spacing:-.15px">{html.escape(_price(row['price'], row['currency'], row['price_usd']))}</p>
   {f'<p style="margin:0 0 15px;color:#7b8984;font-size:11px">{html.escape(remaining)}</p>' if remaining else '<div style="height:15px"></div>'}
   <p style="margin:0 0 14px;color:#71807a;font-size:11px;line-height:1.55">Category: <strong>{html.escape(category)}</strong><br>Classifier used: <strong>{html.escape(classifier)}</strong></p>
   {description_html}<p style="margin:0 0 17px;color:#294442;font-size:13px;line-height:1.5">{html.escape(reason)}</p>
   <p style="margin:0;font-size:12px"><a style="display:inline-block;padding:9px 15px;background:#d7ed62;color:#182b2b;font-weight:700;text-decoration:none" href="{html.escape(like, quote=True)}">Like</a>&nbsp;&nbsp;<a style="display:inline-block;padding:8px 14px;border:1px solid #a9b9b2;color:#55706b;text-decoration:none" href="{html.escape(dislike, quote=True)}">Dislike</a></p>
 </td></tr></table></td></tr></table>''')
    if not rows:
        text.append("No matching listings.")
        blocks.append('<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse:collapse;background:#f7f5ee;border:1px solid #dce6e1"><tr><td style="padding:42px 28px 50px;color:#55706b;font-family:Georgia,serif;font-size:22px">No matching listings.</td></tr></table>')
    usage = usage or {"prompt_tokens": 0, "completion_tokens": 0, "cache_read_tokens": 0}
    input_cost, output_cost, cache_read_cost, total_cost = _usage_cost(usage)
    cost_line = f"Estimated LLM cost: ${total_cost:.6f} (input ${input_cost:.6f}, output ${output_cost:.6f}, cache read ${cache_read_cost:.6f})"
    text.extend(["", cost_line])
    blocks.append(f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse:collapse;background:#182b2b"><tr><td style="padding:18px 28px;color:#b9c7c0;font-size:11px">{html.escape(cost_line)}</td></tr></table>')
    translation_chars = (translation_usage or {}).get("characters", 0)
    if translation_chars:
        translation_line = f"Google Translation usage: {translation_chars:,} source characters this run"
        text.append(translation_line)
        blocks.append(f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border-collapse:collapse;background:#182b2b"><tr><td style="padding:0 28px 18px;color:#b9c7c0;font-size:11px">{html.escape(translation_line)}</td></tr></table>')
    blocks.append("</div></div>")
    return "\n".join(text), "".join(blocks)


def fetch_rows(conn, start: datetime, include_filtered: bool = False) -> list[dict]:
    rows = conn.execute("""select l.source, l.external_id, l.title, l.price, l.currency, l.price_usd,
        l.description, l.url, l.image_urls, l.sale_end_at, l.filter_status, l.filter_reason,
        j.title_reason, j.title_pass, j.category, j.taste_verdict, j.taste_reason
        from listings l left join ai_judgments j on j.listing_id = l.id
        where l.fetched_at >= %s and ((l.filter_status = 'passed' and j.title_pass = true and j.taste_verdict in ('like', 'uncertain'))
          or (%s and l.filter_status = 'filtered'))
        order by case when l.filter_status = 'passed' then 0 else 1 end, l.source, l.fetched_at desc""", (start, include_filtered)).fetchall()
    keys = ("source", "external_id", "title", "price", "currency", "price_usd", "description", "url", "image_urls", "sale_end_at", "filter_status", "filter_reason", "title_reason", "title_pass", "category", "taste_verdict", "taste_reason")
    output = [dict(zip(keys, row)) for row in rows]
    for row in output:
        row["section"] = "Passed" if row["filter_status"] == "passed" else "Filtered"
        if row["section"] == "Filtered":
            row["taste_verdict"] = "filtered"
    return output


def fetch_usage(conn, start: datetime) -> dict:
    row = conn.execute("select coalesce(sum(prompt_tokens), 0), coalesce(sum(completion_tokens), 0), coalesce(sum(total_tokens), 0), coalesce(sum(cache_read_tokens), 0) from llm_usage where recorded_at >= %s", (start,)).fetchone()
    return {"prompt_tokens": row[0], "completion_tokens": row[1], "total_tokens": row[2], "cache_read_tokens": row[3]}


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
            content_length = response.headers.get("content-length")
            if (not content_type.startswith("image/")
                    or (content_length and int(content_length) > MAX_IMAGE_BYTES)
                    or len(response.content) > MAX_IMAGE_BYTES):
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
    if not dry_run:
        conn.execute(
            "select pg_advisory_xact_lock(hashtext(%s))",
            (f"{start.date()}:{recipient}",),
        )
    if not dry_run and conn.execute(
        "select 1 from digest_runs where digest_date = %s and recipient = %s",
        (start.date(), recipient),
    ).fetchone():
        return 0
    feedback_recipient = os.environ.get("IMAP_USERNAME") or recipient
    image_sources, attachments = download_images(rows) if not dry_run else ({}, [])
    translation_usage = {}
    translate_rows(conn, rows, translation_usage)
    text, markup = render(rows, recipient, start, feedback_recipient, fetch_usage(conn, start), image_sources, translation_usage)
    message = EmailMessage()
    message["Subject"] = f"Tastemaker Digest: {len(rows)} matches"
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
