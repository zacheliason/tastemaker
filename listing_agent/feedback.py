from __future__ import annotations

import hashlib
import imaplib
import logging
import os
import tempfile
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parseaddr

import httpx

from .config import required_env
from .storage import SupabaseStorage

logger = logging.getLogger(__name__)
VALID_CATEGORIES = {"art", "home_decor", "clothing"}


def _text(value: str | None) -> str:
    return str(make_header(decode_header(value or "")))


def parse_message(raw: bytes) -> dict | None:
    message = message_from_bytes(raw)
    prefix = "Listing feedback: "
    subject = _text(message.get("Subject"))
    if not subject.lower().startswith(prefix.lower()):
        return None
    action = subject[len(prefix):].strip().lower()
    if action not in {"like", "dislike"}:
        return None
    parts = message.walk() if message.is_multipart() else (message,)
    payloads = [part.get_payload(decode=True) or b"" for part in parts
                if part.get_content_type() == "text/plain" and not part.get_filename()]
    fields = {}
    for line in b"\n".join(payloads).decode("utf-8", errors="replace").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"source", "external_id", "title"}:
            fields[key] = value.strip()
    if not all(fields.get(key) for key in ("source", "external_id", "title")):
        return None
    return {"action": action, **fields, "message_id": message.get("Message-ID") or hashlib.sha256(raw).hexdigest()}


def ingest(conn, storage: SupabaseStorage | None = None, bucket: str = "taste-references") -> int:
    env = required_env("IMAP_HOST", "IMAP_USERNAME", "IMAP_PASSWORD")
    folder = os.environ.get("IMAP_FOLDER", "INBOX")
    logger.info("Starting feedback email intake: host=%s folder=%s", env["IMAP_HOST"], folder)
    mailbox = imaplib.IMAP4_SSL(env["IMAP_HOST"], int(os.environ.get("IMAP_PORT", "993")))
    added = 0
    scanned = 0
    parsed = 0
    duplicates = 0
    skipped = 0
    try:
        mailbox.login(env["IMAP_USERNAME"], env["IMAP_PASSWORD"])
        mailbox.select(folder, readonly=True)
        status, data = mailbox.search(None, "ALL")
        if status != "OK":
            raise RuntimeError("IMAP feedback search failed")
        message_numbers = data[0].split()
        logger.info("Feedback mailbox search complete: messages=%d", len(message_numbers))
        for number in message_numbers:
            scanned += 1
            status, fetched = mailbox.fetch(number, "(RFC822)")
            if status != "OK":
                skipped += 1
                logger.warning("Skipping feedback email: message fetch failed")
                continue
            raw = next((item[1] for item in fetched if isinstance(item, tuple)), None)
            feedback = parse_message(raw) if raw else None
            if not feedback:
                skipped += 1
                continue
            sender = message_from_bytes(raw).get("From", "") if raw else ""
            allowed_sender = os.environ.get("FEEDBACK_FROM") or os.environ.get("DIGEST_FROM") or env["IMAP_USERNAME"]
            if parseaddr(sender)[1].lower() != parseaddr(allowed_sender)[1].lower():
                skipped += 1
                logger.warning("Ignoring feedback from untrusted sender: %s", sender)
                continue
            parsed += 1
            logger.info(
                "Parsed feedback email: action=%s source=%s external_id=%s",
                feedback["action"], feedback["source"], feedback["external_id"],
            )
            event_key = hashlib.sha256(f"{feedback['message_id']}\0{feedback['action']}".encode()).hexdigest()
            if conn.execute("select 1 from feedback_events where event_key = %s", (event_key,)).fetchone():
                duplicates += 1
                logger.info("Skipping already-ingested feedback: action=%s source=%s external_id=%s",
                            feedback["action"], feedback["source"], feedback["external_id"])
                continue
            listing = conn.execute(
                "select l.id, l.image_urls, j.category, l.raw_data from listings l left join ai_judgments j on j.listing_id = l.id where l.source = %s and l.external_id = %s",
                (feedback["source"], feedback["external_id"]),
            ).fetchone()
            if not listing:
                skipped += 1
                logger.warning("Ignoring feedback for unknown listing: %s/%s", feedback["source"], feedback["external_id"])
                continue
            category = listing[2] or (listing[3] or {}).get("_search_config", {}).get("category")
            image_url = (listing[1] or [None])[0]
            if category not in VALID_CATEGORIES or not image_url or not image_url.startswith("https://"):
                skipped += 1
                logger.warning("Ignoring feedback without category or usable image: %s", feedback["external_id"])
                continue
            try:
                response = httpx.get(image_url, timeout=30, follow_redirects=True)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                skipped += 1
                logger.warning("Ignoring feedback image download failure: %s (%s)", feedback["external_id"], exc)
                continue
            content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0]
            if not content_type.startswith("image/") or len(response.content) > 5 * 1024 * 1024:
                skipped += 1
                logger.warning("Ignoring feedback with unusable image response: external_id=%s content_type=%s bytes=%d",
                               feedback["external_id"], content_type, len(response.content))
                continue
            digest = hashlib.sha256(response.content).hexdigest()
            suffix = "." + content_type.split("/", 1)[1].replace("jpeg", "jpg")
            storage_path = f"feedback/{digest}{suffix}"
            with tempfile.NamedTemporaryFile(suffix=suffix) as image:
                image.write(response.content)
                image.flush()
                (storage or SupabaseStorage()).upload(bucket, storage_path, image.name, content_type)
            conn.execute("""insert into taste_references
                (category, label, image_path, source_image_path, image_sha256, mime_type, storage_bucket, storage_path, description)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict (image_sha256) do update set label=excluded.label, category=excluded.category,
                  storage_bucket=excluded.storage_bucket, storage_path=excluded.storage_path""",
                (category, feedback["action"], storage_path, image_url, digest, content_type, bucket, storage_path, feedback["title"]),
            )
            conn.execute("insert into feedback_events (event_key, message_id, listing_id, action, source, external_id) values (%s,%s,%s,%s,%s,%s)",
                         (event_key, feedback["message_id"], listing[0], feedback["action"], feedback["source"], feedback["external_id"]))
            added += 1
            logger.info("Ingested feedback: action=%s source=%s external_id=%s category=%s",
                        feedback["action"], feedback["source"], feedback["external_id"], category)
    finally:
        try:
            mailbox.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
    logger.info("Feedback email intake complete: scanned=%d parsed=%d added=%d duplicates=%d skipped=%d",
                scanned, parsed, added, duplicates, skipped)
    return added
