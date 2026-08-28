from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import configured_sources, enabled_searches, required_env
from .storage import SupabaseStorage


MODEL = "gpt-5.6-luna"


class OpenAIJudge:
    def __init__(self, api_key: str | None = None, model: str | None = None, max_completion_tokens: int = 80):
        self.api_key = api_key or required_env("OPENAI_API_KEY")["OPENAI_API_KEY"]
        self.model = model or os.environ.get("OPENAI_MODEL", MODEL)
        self.max_completion_tokens = int(os.environ.get("OPENAI_MAX_COMPLETION_TOKENS", max_completion_tokens))
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def complete(self, messages: list[dict[str, Any]]) -> Any:
        response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "max_completion_tokens": self.max_completion_tokens,
            },
            timeout=120,
        )
        if response.is_error:
            raise RuntimeError(f"OpenAI request failed ({response.status_code}): {response.text[:500]}")
        payload = response.json()
        usage = payload.get("usage") or {}
        self.last_usage = {key: int(usage.get(key, 0)) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        content = payload["choices"][0]["message"]["content"]
        return json.loads(content)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def title_gate(judge: OpenAIJudge, listings: list[dict], searches: dict[str, dict], instructions: str | None = None) -> dict[str, dict]:
    if not listings:
        return {}
    payload = [{
        "external_id": row["external_id"],
        "title": row["title"],
        "description": row.get("description") or "",
        "size_fields": row.get("size_fields") or {},
        "search": searches.get(row["search_id"], {}),
    } for row in listings]
    result = judge.complete([{
        "role": "system",
        "content": instructions or "Screen listings against their configured search. Return JSON object with key results, an array of objects containing external_id, pass (boolean), and reason (one sentence). Reject clear title/spec mismatches; do not invent missing facts.",
    }, {"role": "user", "content": json.dumps(payload)}])
    return {str(item["external_id"]): {"pass": bool(item.get("pass")), "reason": item.get("reason", "")} for item in result.get("results", [])}


def taste_judgment(judge: OpenAIJudge, listing: dict, references: list[dict], instructions: str | None = None) -> dict:
    content: list[dict[str, Any]] = [{
        "type": "text",
        "text": instructions or "Judge this listing against the supplied reference images for the same category. Return JSON with verdict exactly like, dislike, or uncertain, and one concise reason. Use only visual evidence and the references; do not judge price or brand popularity.",
    }, {"type": "text", "text": json.dumps({"listing": listing, "references": [{"label": r["label"], "description": r.get("description", "")} for r in references]})}]
    for image_url in listing.get("image_urls", []):
        content.append({"type": "image_url", "image_url": {"url": image_url, "detail": "low"}})
    for reference in references:
        content.append({"type": "image_url", "image_url": {"url": reference["url"], "detail": "low"}})
    result = judge.complete([{"role": "user", "content": content}])
    verdict = result.get("verdict", "uncertain")
    if verdict not in {"like", "dislike", "uncertain"}:
        verdict = "uncertain"
    return {"verdict": verdict, "reason": result.get("reason", "")}


def is_fast_tracked(listing: dict, ai_config: dict) -> bool:
    policy = ai_config.get("pre_llm_policy", {}).get(listing["source"], {})
    subject = (listing.get("raw_data") or {}).get("email_subject", "").lower()
    return any(term.lower() in subject for term in policy.get("fast_track_subject_contains", []))


def run_with_config(conn, config: dict, ai_config: dict, source: str | None = None) -> int:
    storage = SupabaseStorage()
    source_clause = "" if source is None else " and source = %s"
    params = () if source is None else (source,)
    rows = conn.execute("select id, source, search_id, external_id, title, description, size_fields, image_urls, raw_data from listings where filter_status = 'passed'" + source_clause, params).fetchall()
    listings = [{"id": r[0], "source": r[1], "search_id": r[2], "external_id": r[3], "title": r[4], "description": r[5], "size_fields": r[6] or {}, "image_urls": r[7] or [], "raw_data": r[8] or {}} for r in rows]
    search_map = {item["id"]: item for _, settings in configured_sources(config) for item in enabled_searches(settings)}
    llm_listings = [listing for listing in listings if not is_fast_tracked(listing, ai_config)]
    judge = OpenAIJudge(model=ai_config.get("model", MODEL), max_completion_tokens=ai_config.get("max_completion_tokens", 80)) if llm_listings else None
    title_results = title_gate(judge, llm_listings, search_map, ai_config.get("title_gate", {}).get("instructions")) if judge else {}
    if judge and llm_listings:
        usage = judge.last_usage
        conn.execute("insert into llm_usage (listing_id, operation, model, prompt_tokens, completion_tokens, total_tokens, listing_count) values (%s,'title_gate',%s,%s,%s,%s,%s)", (llm_listings[0]["id"], judge.model, usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"], len(llm_listings)))
    processed = 0
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
            category = search_map.get(listing["search_id"], {}).get("category", "art")
            refs = conn.execute("select label, description, storage_bucket, storage_path from taste_references where category = %s and active order by label, id limit 20", (category,)).fetchall()
            references = [{"label": r[0], "description": r[1], "url": storage.signed_url(r[2], r[3])} for r in refs if r[2] and r[3]]
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
