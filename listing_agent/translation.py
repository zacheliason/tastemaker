from __future__ import annotations

import hashlib
import logging
import os
from datetime import date

import httpx
from langdetect import DetectorFactory, LangDetectException, detect_langs


logger = logging.getLogger(__name__)
DetectorFactory.seed = 0
GOOGLE_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
MONTHLY_CHARACTER_LIMIT = 500_000
TRANSLATION_BATCH_SIZE = 50


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _language(value: str) -> str | None:
    try:
        candidates = detect_langs(value)
    except LangDetectException:
        return None
    return candidates[0].lang.lower() if candidates else None


def _cached(conn, hashes: list[str]) -> dict[str, tuple[str, str]]:
    if not hashes:
        return {}
    rows = conn.execute(
        "select content_sha256, source_language, translated_text from description_translations "
        "where content_sha256 = any(%s) and target_language = 'EN'",
        (hashes,),
    ).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def _monthly_usage(conn, month_start: date) -> int:
    row = conn.execute(
        "select chars_translated from translation_usage_monthly where month_start = %s",
        (month_start,),
    ).fetchone()
    return row[0] if row else 0


def _record_monthly_usage(conn, month_start: date, chars: int) -> None:
    conn.execute(
        "insert into translation_usage_monthly (month_start, chars_translated) values (%s, %s) "
        "on conflict (month_start) do update set chars_translated = translation_usage_monthly.chars_translated + excluded.chars_translated",
        (month_start, chars),
    )


def _translate_batch(texts: list[str]) -> list[tuple[str, str]]:
    response = httpx.post(
        os.environ.get("GOOGLE_TRANSLATE_URL", GOOGLE_TRANSLATE_URL),
        params={"key": os.environ["GOOGLE_TRANSLATE_API_KEY"]},
        data=[("q", text) for text in texts] + [("target", "en"), ("format", "html")],
        timeout=30,
    )
    response.raise_for_status()
    translations = response.json()["data"]["translations"]
    return [
        (item.get("detectedSourceLanguage", "UNKNOWN").upper(), item["translatedText"])
        for item in translations
    ]


def translate_rows(conn, rows: list[dict], usage: dict | None = None) -> int:
    """Translate displayed descriptions while enforcing the monthly free allowance."""
    usage = usage if usage is not None else {}
    usage["characters"] = 0
    candidates = []
    for row in rows:
        if row.get("section", "Passed") != "Passed":
            continue
        original = (row.get("description") or "").strip()
        if not original or not any(char.isalpha() for char in original) or _language(original) == "en":
            continue
        candidates.append((row, original, _content_hash(original)))

    if not candidates:
        return 0
    cached = _cached(conn, [item[2] for item in candidates])
    pending = [item for item in candidates if item[2] not in cached]
    if pending and not os.environ.get("GOOGLE_TRANSLATE_API_KEY"):
        logger.warning("GOOGLE_TRANSLATE_API_KEY is not set; leaving non-English descriptions unchanged")
        pending = []

    month_start = date.today().replace(day=1)
    if pending:
        # Serialize allowance checks across overlapping scheduled/manual runs.
        conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"translation:{month_start}",))
    remaining = max(0, MONTHLY_CHARACTER_LIMIT - _monthly_usage(conn, month_start))
    if pending and not remaining:
        logger.warning("Google Translation monthly character limit reached; translation disabled")

    for offset in range(0, len(pending), TRANSLATION_BATCH_SIZE):
        batch = pending[offset:offset + TRANSLATION_BATCH_SIZE]
        allowed = 0
        used = 0
        for item in batch:
            if used + len(item[1]) > remaining:
                break
            used += len(item[1])
            allowed += 1
        batch = batch[:allowed]
        if not batch:
            break
        # Reserve before the request so a failed/interrupted request cannot cause
        # a later run to exceed the hard monthly limit.
        _record_monthly_usage(conn, month_start, used)
        usage["characters"] += used
        remaining -= used
        try:
            translated = _translate_batch([item[1] for item in batch])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            logger.warning("description translation batch failed: %s", exc)
            continue
        if len(translated) != len(batch):
            logger.warning("description translation batch returned an unexpected item count")
            continue
        for (_, _, content_hash), (source_language, translated_text) in zip(batch, translated):
            cached[content_hash] = (source_language, translated_text)
            conn.execute(
                "insert into description_translations "
                "(content_sha256, source_language, target_language, translated_text) "
                "values (%s, %s, 'EN', %s) on conflict (content_sha256, target_language) do update "
                "set source_language = excluded.source_language, translated_text = excluded.translated_text, translated_at = now()",
                (content_hash, source_language, translated_text),
            )
        if not remaining:
            logger.warning("Google Translation monthly character limit reached; translation disabled")
            break

    translated_count = 0
    for row, _, content_hash in candidates:
        result = cached.get(content_hash)
        if result:
            source_language, translated_text = result
            row["description"] = f"(TRANSLATED FROM {source_language}: {translated_text})"
            translated_count += 1
    return translated_count
