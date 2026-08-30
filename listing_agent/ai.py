from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import configured_sources, enabled_searches, required_env
from .storage import SupabaseStorage


MODEL = "gpt-5.6-luna"
logger = logging.getLogger(__name__)
TASTE_CATEGORIES = {"art", "home_decor", "clothing"}


class OpenAIJudge:
    def __init__(self, api_key: str | None = None, model: str | None = None, max_completion_tokens: int = 80):
        self.api_key = api_key or required_env("OPENAI_API_KEY")["OPENAI_API_KEY"]
        self.model = model or os.environ.get("OPENAI_MODEL", MODEL)
        self.max_completion_tokens = int(os.environ.get("OPENAI_MAX_COMPLETION_TOKENS", max_completion_tokens))
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def complete(self, messages: list[dict[str, Any]]) -> Any:
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        retry_messages = list(messages)
        budgets = [self.max_completion_tokens, max(self.max_completion_tokens * 2, 160), max(self.max_completion_tokens * 4, 320)]
        for attempt, budget in enumerate(budgets):
            response = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "messages": retry_messages,
                    "response_format": {"type": "json_object"},
                    "max_completion_tokens": budget,
                },
                timeout=120,
            )
            if response.is_error:
                raise RuntimeError(f"OpenAI request failed ({response.status_code}): {response.text[:500]}")
            payload = response.json()
            usage = payload.get("usage") or {}
            for key in total_usage:
                total_usage[key] += int(usage.get(key, 0))
            choice = (payload.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
            finish_reason = choice.get("finish_reason", "unknown")
            logger.info(
                "OpenAI completion attempt=%d model=%s finish_reason=%s prompt_tokens=%s completion_tokens=%s content_chars=%d response_id=%s",
                attempt + 1, self.model, finish_reason, usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0), len(content), payload.get("id", "unknown"),
            )
            try:
                parsed = json.loads(content.strip())
                self.last_usage = total_usage
                return parsed
            except (json.JSONDecodeError, TypeError):
                if attempt == len(budgets) - 1:
                    logger.error(
                        "OpenAI returned invalid JSON after %d attempts: model=%s finish_reason=%s content_chars=%d response_id=%s content=%r",
                        len(budgets), self.model, finish_reason, len(content), payload.get("id", "unknown"), content[:2000],
                    )
                    raise RuntimeError(
                        f"OpenAI returned invalid JSON (finish_reason={finish_reason}, content_chars={len(content)}, "
                        f"response_id={payload.get('id', 'unknown')})"
                    )
                retry_messages = list(messages) + [{
                    "role": "user",
                    "content": "Return only the requested JSON object. No prose, markdown, or empty response. Keep it compact.",
                }]


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def title_gate(judge: OpenAIJudge, listings: list[dict], searches: dict[str, dict], instructions: str | None = None, on_batch=None) -> dict[str, dict]:
    if not listings:
        return {}
    batch_size = max(1, int(os.environ.get("OPENAI_TITLE_BATCH_SIZE", "5")))
    results = {}
    for start in range(0, len(listings), batch_size):
        batch = listings[start:start + batch_size]
        try:
            batch_results = _title_gate_batch(judge, batch, searches, instructions, start // batch_size + 1)
        except RuntimeError as exc:
            if len(batch) > 1:
                logger.warning("OpenAI title gate batch failed; retrying %d listings individually: %s", len(batch), exc)
                batch_results = {}
                for row in batch:
                    try:
                        batch_results.update(_title_gate_batch(judge, [row], searches, instructions, row["external_id"]))
                    except RuntimeError as item_exc:
                        logger.error("OpenAI title gate failed for one listing; treating as rejected: external_id=%s error=%s", row["external_id"], item_exc)
            else:
                logger.error("OpenAI title gate failed; treating listing as rejected: external_id=%s error=%s", batch[0]["external_id"], exc)
                batch_results = {}
        results.update(batch_results)
        if on_batch:
            on_batch(batch, batch_results)
    return results


def _title_gate_batch(judge: OpenAIJudge, batch: list[dict], searches: dict[str, dict],
                      instructions: str | None, batch_label: int | str) -> dict[str, dict]:
        """Run one title batch; callers decide whether a failed batch is recoverable."""
        payload = [{
            "external_id": row["external_id"],
            "title": row["title"],
            "description": row.get("description") or "",
            "size_fields": row.get("size_fields") or {},
            "search": row.get("search") or searches.get(row["search_id"], {}),
        } for row in batch]
        logger.info("OpenAI title gate batch=%s listings=%d", batch_label, len(batch))
        result = judge.complete([{
            "role": "system",
            "content": (instructions or "Screen listings against their configured search. Return JSON object with key results, an array of objects containing external_id, pass (boolean), reason (one sentence), and category. For category, choose exactly one of art, home_decor, or clothing based on the title. Reject clear title/spec mismatches; do not invent missing facts.") + " Assign each listing exactly one category: art, home_decor, or clothing, based on its title.",
        }, {"role": "user", "content": json.dumps(payload)}])
        output = {}
        for item in result.get("results", []):
            category = item.get("category")
            result_item = {"pass": bool(item.get("pass")), "reason": item.get("reason", "")}
            if category in TASTE_CATEGORIES:
                result_item["category"] = category
            output[str(item["external_id"])] = result_item
        return output


def taste_judgment(judge: OpenAIJudge, listing: dict, references: list[dict], instructions: str | None = None) -> dict:
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": instructions or "Judge this listing against the supplied reference images for the same category. Return JSON with verdict exactly like, dislike, or uncertain, and one concise reason. Use only visual evidence and the references; do not judge price or brand popularity.",
    }, {"type": "text", "text": json.dumps({"listing": listing, "references": [{"label": r["label"], "description": r.get("description", "")} for r in references]})}]
    for image_url in listing.get("image_urls", []):
        content.append({"type": "image_url", "image_url": {"url": image_url, "detail": "low"}})
    for reference in references:
        content.append({"type": "image_url", "image_url": {"url": reference["url"], "detail": "low"}})
    try:
        result = judge.complete([{"role": "user", "content": content}])
    except RuntimeError as exc:
        logger.error("OpenAI taste judgment failed; treating listing as uncertain: error=%s", exc)
        return {"verdict": "uncertain", "reason": "AI judgment unavailable."}
    verdict = result.get("verdict", "uncertain")
    if verdict not in {"like", "dislike", "uncertain"}:
        verdict = "uncertain"
    return {"verdict": verdict, "reason": result.get("reason", "")}


def _preference_classifier(conn, storage, category: str, cache: dict):
    from .embeddings import fit_preference_classifier, image_embedding

    if category in cache:
        return cache[category]
    rows = conn.execute(
        "select id, label, storage_bucket, storage_path, embedding from taste_references "
        "where category = %s and active order by id", (category,)
    ).fetchall()
    samples = []
    for row in rows:
        vector = row[4]
        if vector is None and row[2] and row[3]:
            try:
                vector = image_embedding(storage.signed_url(row[2], row[3]))
                conn.execute("update taste_references set embedding = %s::jsonb where id = %s", (json.dumps(vector), row[0]))
            except Exception as exc:
                logger.warning("Reference embedding failed: category=%s id=%s error=%s", category, row[0], exc)
        if vector is not None and row[1] in {"like", "dislike"}:
            samples.append((vector, row[1]))
    classifier = fit_preference_classifier(samples)
    cache[category] = classifier
    logger.info("Taste classifier: category=%s examples=%d available=%s", category, len(samples), bool(classifier))
    return classifier


def embedding_taste_judgment(conn, storage, listing: dict, category: str, classifier) -> dict:
    from .embeddings import image_embedding

    if classifier is None:
        return {"verdict": "uncertain", "reason": "No labeled references available."}
    image_urls = listing.get("image_urls") or []
    if not image_urls:
        return {"verdict": "uncertain", "reason": "Listing has no image."}
    try:
        verdict, probability = classifier.predict(image_embedding(image_urls[0]))
    except Exception as exc:
        logger.warning("Listing embedding failed: external_id=%s error=%s", listing.get("external_id"), exc)
        return {"verdict": "uncertain", "reason": "Image embedding unavailable."}
    return {"verdict": verdict, "reason": f"Embedding classifier confidence {probability:.0%}."}


def _nearest_references(conn, storage, listing: dict, category: str, limit: int, pool_limit: int, cache: dict) -> list[dict]:
    from .embeddings import cosine_similarity, image_embedding

    if category not in cache:
        rows = conn.execute("select id, label, description, storage_bucket, storage_path, embedding from taste_references where category = %s and active order by label, id limit %s", (category, pool_limit)).fetchall()
        references = []
        for row in rows:
            if not row[3] or not row[4]:
                continue
            reference = {"id": row[0], "label": row[1], "description": row[2], "url": storage.signed_url(row[3], row[4]), "embedding": row[5]}
            if reference["embedding"] is None:
                try:
                    reference["embedding"] = image_embedding(reference["url"])
                    conn.execute("update taste_references set embedding = %s::jsonb where id = %s", (json.dumps(reference["embedding"]), reference["id"]))
                except Exception as exc:
                    logger.warning("Reference embedding failed; using reference order: category=%s id=%s error=%s", category, reference["id"], exc)
            references.append(reference)
        cache[category] = references

    references = cache[category]
    image_urls = listing.get("image_urls") or []
    if not image_urls or not references:
        return references[:limit]
    try:
        listing_embedding = image_embedding(image_urls[0])
        ranked = sorted(
            references,
            key=lambda reference: cosine_similarity(listing_embedding, reference["embedding"])
            if reference.get("embedding") else float("-inf"),
            reverse=True,
        )
        return ranked[:limit]
    except Exception as exc:
        logger.warning("Listing embedding failed; using reference order: external_id=%s error=%s", listing.get("external_id"), exc)
        return references[:limit]


def is_fast_tracked(listing: dict, ai_config: dict) -> bool:
    policy = ai_config.get("pre_llm_policy", {}).get(listing["source"], {})
    subject = (listing.get("raw_data") or {}).get("email_subject", "").lower()
    return any(term.lower() in subject for term in policy.get("fast_track_subject_contains", []))


def _title_hash(listing: dict, search: dict, instructions: str | None, model: str) -> str:
    # Do not fingerprint raw_data: marketplace payloads commonly change metadata
    # between fetches without changing the title-gate decision inputs.
    return _digest({
        "listing": {
            "title": listing.get("title") or "",
            "description": listing.get("description") or "",
            "size_fields": listing.get("size_fields") or {},
        },
        "search": search,
        "instructions": instructions,
        "model": model,
    })


def _taste_hash(listing: dict, category: str, classifier_state: Any) -> str:
    # Taste classification uses the listing images and reference classifier;
    # volatile source metadata must not invalidate an unchanged judgment.
    return _digest({
        "image_urls": listing.get("image_urls") or [],
        "category": category,
        "classifier": classifier_state,
    })


def run_with_config(conn, config: dict, ai_config: dict, source: str | None = None) -> int:
    storage = SupabaseStorage()
    source_clause = "" if source is None else " and source = %s"
    params = () if source is None else (source,)
    rows = conn.execute("select id, source, search_id, external_id, title, description, size_fields, image_urls, raw_data from listings where filter_status = 'passed'" + source_clause, params).fetchall()
    listings = [{"id": r[0], "source": r[1], "search_id": r[2], "external_id": r[3], "title": r[4], "description": r[5], "size_fields": r[6] or {}, "image_urls": r[7] or [], "raw_data": r[8] or {}, "search": (r[8] or {}).get("_search_config", {})} for r in rows]
    logger.info("AI candidates: source=%s filter_status=passed count=%d", source or "all", len(listings))
    search_map = {item["id"]: item for _, settings in configured_sources(config) for item in enabled_searches(settings)}
    model = ai_config.get("model", MODEL)
    title_instructions = ai_config.get("title_gate", {}).get("instructions")
    stored_titles = {}
    llm_listings = []
    for listing in listings:
        if is_fast_tracked(listing, ai_config):
            continue
        title_hash = _title_hash(listing, search_map.get(listing["search_id"], {}), title_instructions, model)
        existing = conn.execute("select title_input_sha256, title_pass, title_reason, category, taste_input_sha256, taste_verdict, taste_reason from ai_judgments where listing_id = %s", (listing["id"],)).fetchone()
        if existing and existing[0] == title_hash and existing[1] is not None:
            stored_titles[listing["external_id"]] = {"pass": existing[1], "reason": existing[2] or "", "category": existing[3]}
        else:
            llm_listings.append(listing)
    judge = OpenAIJudge(model=model, max_completion_tokens=ai_config.get("max_completion_tokens", 80)) if llm_listings else None

    def checkpoint_titles(batch, batch_results):
        for listing in batch:
            result = batch_results.get(listing["external_id"])
            if not result:
                continue
            conn.execute("""insert into ai_judgments
                (listing_id, model, title_input_sha256, title_pass, title_reason, category, judged_at)
                values (%s,%s,%s,%s,%s,%s,%s)
                on conflict (listing_id) do update set model=excluded.model,
                title_input_sha256=excluded.title_input_sha256, title_pass=excluded.title_pass,
                title_reason=excluded.title_reason, category=excluded.category, taste_input_sha256=null,
                taste_verdict=null, taste_reason=null, judged_at=excluded.judged_at""", (
                    listing["id"], model, _title_hash(listing, search_map.get(listing["search_id"], {}), title_instructions, model),
                    result["pass"], result["reason"], result.get("category"), datetime.now(timezone.utc),
                ))
        conn.commit()

    title_results = dict(stored_titles)
    if judge:
        title_results.update(title_gate(judge, llm_listings, search_map, title_instructions, checkpoint_titles))
    logger.info("AI title results: candidates=%d results=%d fast_tracked=%d", len(llm_listings), len(title_results), len(listings) - len(llm_listings))
    if judge and llm_listings:
        usage = judge.last_usage
        conn.execute("insert into llm_usage (listing_id, operation, model, prompt_tokens, completion_tokens, total_tokens, listing_count) values (%s,'title_gate',%s,%s,%s,%s,%s)", (llm_listings[0]["id"], judge.model, usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"], len(llm_listings)))
    processed = 0
    skipped = 0
    classifier_cache = {}
    for listing in listings:
        title = title_results.get(listing["external_id"], {"pass": False, "reason": "No title-gate result"})
        if is_fast_tracked(listing, ai_config):
            title = {"pass": True, "reason": "Fast-tracked by configured Invaluable subject rule."}
            title_hash = _digest({"listing": listing, "policy": "fast-track"})
        else:
            title_hash = _title_hash(listing, search_map.get(listing["search_id"], {}), title_instructions, model)
            title = title_results.get(listing["external_id"], {"pass": False, "reason": "No title-gate result"})
        taste = None
        taste_hash = None
        category = None
        if title["pass"]:
            category = title.get("category") or search_map.get(listing["search_id"], {}).get("category")
            if not is_fast_tracked(listing, ai_config) and category in TASTE_CATEGORIES:
                classifier = _preference_classifier(conn, storage, category, classifier_cache)
                classifier_state = classifier.__dict__ if classifier else None
                taste_hash = _taste_hash(listing, category, classifier_state)
        existing = conn.execute("select title_input_sha256, taste_input_sha256 from ai_judgments where listing_id = %s", (listing["id"],)).fetchone()
        if existing and existing[0] == title_hash and (not title["pass"] or existing[1] == taste_hash):
            skipped += 1
            continue
        if is_fast_tracked(listing, ai_config):
            taste = {"verdict": "like", "reason": "Fast-tracked by configured Invaluable subject rule; no LLM used."}
        elif title["pass"]:
            if category not in TASTE_CATEGORIES:
                taste = {"verdict": "uncertain", "reason": "Category could not be assigned"}
                conn.execute("insert into ai_judgments (listing_id, model, title_input_sha256, title_pass, title_reason, category, taste_input_sha256, taste_verdict, taste_reason, judged_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (listing_id) do update set model=excluded.model, title_input_sha256=excluded.title_input_sha256, title_pass=excluded.title_pass, title_reason=excluded.title_reason, category=excluded.category, taste_input_sha256=excluded.taste_input_sha256, taste_verdict=excluded.taste_verdict, taste_reason=excluded.taste_reason, judged_at=excluded.judged_at", (listing["id"], judge.model, title_hash, title["pass"], title["reason"], category, None, taste["verdict"], taste["reason"], datetime.now(timezone.utc)))
                processed += 1
                conn.commit()
                continue
            taste = embedding_taste_judgment(conn, storage, listing, category, classifier)
        logger.info(
            "AI judgment: listing_id=%s external_id=%s title_pass=%s title_reason=%r category=%s taste_verdict=%s taste_reason=%r",
            listing["id"], listing["external_id"], title["pass"], title["reason"],
            title.get("category") or (category if title["pass"] else None),
            taste["verdict"] if taste else None, taste["reason"] if taste else None,
        )
        conn.execute("""insert into ai_judgments (listing_id, model, title_input_sha256, title_pass, title_reason, category, taste_input_sha256, taste_verdict, taste_reason, judged_at)
          values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
          on conflict (listing_id) do update set model=excluded.model, title_input_sha256=excluded.title_input_sha256, title_pass=excluded.title_pass, title_reason=excluded.title_reason, category=excluded.category, taste_input_sha256=excluded.taste_input_sha256, taste_verdict=excluded.taste_verdict, taste_reason=excluded.taste_reason, judged_at=excluded.judged_at""", (listing["id"], judge.model if judge else ai_config.get("model", MODEL), title_hash, title["pass"], title["reason"], category, taste_hash, taste["verdict"] if taste else None, taste["reason"] if taste else None, datetime.now(timezone.utc)))
        processed += 1
        conn.commit()
    persisted = conn.execute(
        "select count(*) from ai_judgments where listing_id = any(%s)",
        ([listing["id"] for listing in listings],),
    ).fetchone()[0] if listings else 0
    logger.info(
        "AI judgments persisted: candidates=%d upserted=%d skipped_unchanged=%d persisted_for_candidates=%d",
        len(listings), processed, skipped, persisted,
    )
    return processed
