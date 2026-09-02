from __future__ import annotations

import hashlib
import logging
import os
from datetime import date
from urllib.parse import urlencode

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
    languages, _ = _language_candidates(value)
    return languages[0].lang.lower() if languages else None


def _language_candidates(value: str):
    try:
        candidates = detect_langs(value)
    except LangDetectException as exc:
        return [], type(exc).__name__
    return candidates, None


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
    logger.info("translation API request: batches=1 descriptions=%d characters=%d", len(texts), sum(len(text) for text in texts))
    response = httpx.post(
        os.environ.get("GOOGLE_TRANSLATE_URL", GOOGLE_TRANSLATE_URL),
        params={"key": os.environ["GOOGLE_TRANSLATE_API_KEY"]},
        content=urlencode(
            [("q", text) for text in texts] + [("target", "en"), ("format", "html")]
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    response.raise_for_status()
    logger.info("translation API response: status=%s descriptions=%d", getattr(response, "status_code", "unknown"), len(response.json().get("data", {}).get("translations", [])))
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
        content_hash = _content_hash(original) if original else None
        if not original:
            logger.info("translation candidate skipped: reason=empty hash=none")
            continue
        if not any(char.isalpha() for char in original):
            logger.info("translation candidate skipped: reason=no_letters hash=%s characters=%d", content_hash[:12], len(original))
            continue
        languages, detection_error = _language_candidates(original)
        language_summary = ",".join(f"{item.lang}:{item.prob:.3f}" for item in languages) or "none"
        logger.info(
            "translation language detection: hash=%s characters=%d candidates=%s error=%s",
            content_hash[:12], len(original), language_summary, detection_error or "none",
        )
        if languages and languages[0].lang.lower() == "en":
            logger.info("translation candidate skipped: reason=english hash=%s", content_hash[:12])
            continue
        candidates.append((row, original, content_hash))

    if not candidates:
        logger.info("translation candidates: total=0")
        return 0
    cached = _cached(conn, [item[2] for item in candidates])
    pending = [item for item in candidates if item[2] not in cached]
    logger.info("translation cache status: candidates=%d cached=%d pending=%d", len(candidates), len(cached), len(pending))
    if pending and not os.environ.get("GOOGLE_TRANSLATE_API_KEY"):
        logger.warning("translation disabled: reason=missing_api_key pending=%d", len(pending))
        pending = []

    month_start = date.today().replace(day=1)
    if pending:
        # Serialize allowance checks across overlapping scheduled/manual runs.
        conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"translation:{month_start}",))
    remaining = max(0, MONTHLY_CHARACTER_LIMIT - _monthly_usage(conn, month_start))
    if pending and not remaining:
        logger.warning("translation disabled: reason=monthly_character_limit pending=%d", len(pending))

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
        except httpx.HTTPStatusError as exc:
            logger.warning("translation batch failed: reason=http_status status=%s descriptions=%d", exc.response.status_code, len(batch))
            continue
        except httpx.RequestError as exc:
            logger.warning("translation batch failed: reason=request_error error=%s descriptions=%d", type(exc).__name__, len(batch))
            continue
        except httpx.HTTPError as exc:
            logger.warning("translation batch failed: reason=http_error error=%s descriptions=%d", type(exc).__name__, len(batch))
            continue
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("translation batch failed: reason=response_shape error=%s descriptions=%d", type(exc).__name__, len(batch))
            continue
        if len(translated) != len(batch):
            logger.warning("translation batch failed: reason=item_count expected=%d actual=%d", len(batch), len(translated))
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
            logger.warning("translation disabled: reason=monthly_character_limit")
            break

    translated_count = 0
    for row, _, content_hash in candidates:
        result = cached.get(content_hash)
        if result:
            source_language, translated_text = result
            row["description"] = f"(TRANSLATED FROM {source_language}: {translated_text})"
            translated_count += 1
    return translated_count
