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


def title_gate(judge: OpenAIJudge, listings: list[dict], searches: dict[str, dict], instructions: str | None = None) -> dict[str, dict]:
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


def run_with_config(conn, config: dict, ai_config: dict, source: str | None = None) -> int:
    storage = SupabaseStorage()
    source_clause = "" if source is None else " and source = %s"
    params = () if source is None else (source,)
    rows = conn.execute("select id, source, search_id, external_id, title, description, size_fields, image_urls, raw_data from listings where filter_status = 'passed'" + source_clause, params).fetchall()
    listings = [{"id": r[0], "source": r[1], "search_id": r[2], "external_id": r[3], "title": r[4], "description": r[5], "size_fields": r[6] or {}, "image_urls": r[7] or [], "raw_data": r[8] or {}, "search": (r[8] or {}).get("_search_config", {})} for r in rows]
    search_map = {item["id"]: item for _, settings in configured_sources(config) for item in enabled_searches(settings)}
    llm_listings = [listing for listing in listings if not is_fast_tracked(listing, ai_config)]
    judge = OpenAIJudge(model=ai_config.get("model", MODEL), max_completion_tokens=ai_config.get("max_completion_tokens", 80)) if llm_listings else None
    title_results = title_gate(judge, llm_listings, search_map, ai_config.get("title_gate", {}).get("instructions")) if judge else {}
    if judge and llm_listings:
        usage = judge.last_usage
        conn.execute("insert into llm_usage (listing_id, operation, model, prompt_tokens, completion_tokens, total_tokens, listing_count) values (%s,'title_gate',%s,%s,%s,%s,%s)", (llm_listings[0]["id"], judge.model, usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"], len(llm_listings)))
    processed = 0
    reference_cache = {}
    reference_limit = max(1, int(ai_config.get("reference_limit", 6)))
    reference_pool_limit = max(reference_limit, int(ai_config.get("reference_pool_limit", 100)))
    for listing in listings:
        title = title_results.get(listing["external_id"], {"pass": False, "reason": "No title-gate result"})
        title_instructions = ai_config.get("title_gate", {}).get("instructions")
        if is_fast_tracked(listing, ai_config):
            title = {"pass": True, "reason": "Fast-tracked by configured Invaluable subject rule."}
            title_hash = _digest({"listing": listing, "policy": "fast-track"})
        else:
            title_hash = _digest({"listing": listing, "search": search_map.get(listing["search_id"], {}), "instructions": title_instructions, "model": judge.model})
            title = title_results.get(listing["external_id"], {"pass": False, "reason": "No title-gate result"})
        existing = conn.execute("select title_input_sha256, taste_input_sha256 from ai_judgments where listing_id = %s", (listing["id"],)).fetchone()
        if existing and existing[0] == title_hash and (not title["pass"] or existing[1]):
            continue
        taste = None
        taste_hash = None
        if is_fast_tracked(listing, ai_config):
            taste = {"verdict": "like", "reason": "Fast-tracked by configured Invaluable subject rule; no LLM used."}
        elif title["pass"]:
            category = title.get("category") or search_map.get(listing["search_id"], {}).get("category")
            if category not in TASTE_CATEGORIES:
                taste = {"verdict": "uncertain", "reason": "Category could not be assigned"}
                conn.execute("insert into ai_judgments (listing_id, model, title_input_sha256, title_pass, title_reason, taste_input_sha256, taste_verdict, taste_reason, judged_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (listing_id) do update set model=excluded.model, title_input_sha256=excluded.title_input_sha256, title_pass=excluded.title_pass, title_reason=excluded.title_reason, taste_input_sha256=excluded.taste_input_sha256, taste_verdict=excluded.taste_verdict, taste_reason=excluded.taste_reason, judged_at=excluded.judged_at", (listing["id"], judge.model, title_hash, title["pass"], title["reason"], None, taste["verdict"], taste["reason"], datetime.now(timezone.utc)))
                processed += 1
                continue
            references = _nearest_references(conn, storage, listing, category, reference_limit, reference_pool_limit, reference_cache)
            taste_instructions = ai_config.get("taste_judgment", {}).get("instructions")
            taste_hash = _digest({"listing": listing, "references": references, "instructions": taste_instructions, "model": judge.model})
            taste = taste_judgment(judge, listing, references, taste_instructions) if references else {"verdict": "uncertain", "reason": "No portable references available"}
            if references:
                usage = judge.last_usage
                conn.execute("insert into llm_usage (listing_id, operation, model, prompt_tokens, completion_tokens, total_tokens) values (%s,'taste_judgment',%s,%s,%s,%s)", (listing["id"], judge.model, usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]))
        conn.execute("""insert into ai_judgments (listing_id, model, title_input_sha256, title_pass, title_reason, taste_input_sha256, taste_verdict, taste_reason, judged_at)
          values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
          on conflict (listing_id) do update set model=excluded.model, title_input_sha256=excluded.title_input_sha256, title_pass=excluded.title_pass, title_reason=excluded.title_reason, taste_input_sha256=excluded.taste_input_sha256, taste_verdict=excluded.taste_verdict, taste_reason=excluded.taste_reason, judged_at=excluded.judged_at""", (listing["id"], judge.model if judge else ai_config.get("model", MODEL), title_hash, title["pass"], title["reason"], taste_hash, taste["verdict"] if taste else None, taste["reason"] if taste else None, datetime.now(timezone.utc)))
        processed += 1
    return processed
