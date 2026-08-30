# Daily Listing Taste-Filter Agent Handoff

This file is the source of truth for continuing work in a fresh LLM conversation. Read it before changing code. The project is at `/Users/zach/Documents/ow`.

## How To Continue

- Inspect `git status`, the recent commits, and this file before editing. Preserve unrelated user or agent changes.
- Never read, print, paste, or commit `.env` or any secret. Use `.env` locally and GitHub Actions secrets remotely.
- Prefer the smallest correct change. Run `python3 -m pytest` after code changes.
- Do not claim a feature is production-verified until a real end-to-end run proves it.
- Do not scrape eBay pages. Use the official eBay Browse API only.
- Do not call Pinterest or Invaluable private endpoints without checking permissions and terms.

## User Decisions

- Sources are Invaluable listing-alert emails and the official eBay Browse API.
- Initial categories are `art`, `home_decor`, and `clothing`.
- State is hosted Postgres, currently Supabase.
- Hosting and scheduling are GitHub Actions, represented by committed workflow YAML.
- OpenAI performs title/spec screening and visual taste judgment.
- The lightweight model is referred to as `gpt-5.6 Luna`, but the exact API model identifier must be verified live before relying on it.
- eBay test search: `ralph lauren polo cream pleated trousers waist size 30`.
- eBay deterministic price limit: `max_price_usd: 100`.
- eBay title/spec screening should happen before vision judgment and should batch listings where practical.
- Invaluable currently accepts all listing-like items from the dedicated inbox. The old artist/woodblock/$300 filter is not a requirement.
- References must remain category-specific. Never compare art against clothing or home decor.
- Import all available Pinterest positive examples. Dislikes will come from later feedback.
- Invaluable enrichment is useful but must never replace baseline email ingestion.
- The final output is one consolidated daily email digest, grouped by source.
- Feedback should use pre-addressed Like/Dislike email replies rather than public web callbacks.

## Current Repository

- `listing_agent/models.py`: shared `Listing` dataclass with source, search, price/currency, USD conversion, URL, images, description, size fields, raw data, and sale end time.
- `listing_agent/ebay.py`: OAuth token acquisition, Browse API search, and authenticated `GetMyeBayBuying` saved-search parsing. It supports refresh-token and client-credentials flows.
- `listing_agent/invaluable.py`: IMAP ingestion, HTML alert parsing, JSON-LD lot parsing, ZenRows enrichment, retry/fallback metadata, and existing-URL skipping.
- `listing_agent/zenrows.py`: authorized ZenRows enrichment adapter with JavaScript rendering.
- `listing_agent/pricing.py`: price parsing and FreeCurrencyAPI conversion to USD.
- `listing_agent/db.py`: normalized, idempotent Postgres upserts and URL normalization.
- `listing_agent/filters.py`: deterministic exclusion-keyword, USD-price, and structured-size filters with audit fields.
- `listing_agent/references.py`: local metadata import plus normalized-image upload/reconciliation into Supabase Storage.
- `listing_agent/storage.py`: private Supabase Storage upload and signed-URL client.
- `listing_agent/ai.py`: OpenAI JSON client with malformed/empty JSON retry, batched title gate, category-specific vision judgment, fast-track policy, judgment hashes, and usage logging.
- `listing_agent/digest.py`: grouped HTML/text email rendering, inline transient listing-image attachments, USD/sale timing display, usage footer, and Like/Dislike mailto links.
- `listing_agent/cli.py`: `ingest`, `filter`, `import-references`, `upload-references`, `judge`, `digest`, and `enrich-url` commands.
- `scripts/normalize_images.py`: converts reference inputs to JPEG and writes a normalization manifest.
- `db/schema.sql`: idempotent schema/migrations for listings, references, AI judgments, digest runs, and LLM usage.
- `config/searches.json`: source registry and search configuration.
- `config/ai.json`: model, prompts, fast-track rules, and completion budget.
- `config/digest.json`: filtered-listing validation toggle.
- `.github/workflows/daily-listings.yml`: scheduled/manual pipeline skeleton.
- `README.md`: remote setup and operating documentation.

## Verified Progress

### Local and Database Verification

- Gmail IMAP connection succeeded.
- A forwarded Invaluable alert titled `Fwd: New pieces from Josef Albers and Sabra Field added` parsed correctly.
- Three real Invaluable listings were previously upserted into Supabase: Josef Albers, Sabra Field woodblock triptych, and Sabra Johnson Field woodblock.
- Supabase Session Pooler connection works.
- Schema and additive migrations were previously applied manually through psycopg because `psql` is not installed locally.
- A real filter run previously reported `invaluable: before=3 passed=3 filtered=0`.
- The Invaluable email HTML fix handles `img[alt="lot image"]` and separate title cells.

### Enrichment Verification

- The Josef Albers lot URL once returned valid JSON-LD through browser-based enrichment:

```text
title: Josef Albers (1888 Bottrop - New Haven 1976)
price: 2000 EUR
external_id: e978405423
images: 1
```

- Ordinary `httpx` hit an Invaluable CloudFront/WAF challenge.
- ZenRows support is implemented but has not yet been proven against a real authorized remote run.
- Enrichment defaults to two attempts in the current code, not four. Confirm whether two is sufficient before changing it.

### References Verification

- The initial local import stored 316 unique positive metadata rows in Supabase: `art: 269`, `clothing: 23`, `home_decor: 24`.
- `/Users/zach/Downloads/inspo_normalized` was normalized successfully: 338 JPEG files, 47 MB, maximum dimension 1600px, zero failures.
- Supabase Storage upload/reconciliation code and unit tests now exist.
- Actual upload of the current 338-file set to the private `taste-references` bucket has not been verified in the current state.
- The original database paths point to local files until upload/reconciliation is run successfully.

### Code and Test Verification

- Tests cover Invaluable parsing, pricing, URLs, ZenRows request shape, eBay saved-search parsing, reference storage, AI JSON retry/judgment behavior, and digest rendering/delivery guards.
- Run the full suite with:

```sh
python3 -m pytest
```

- The old handoff said `4 passed`; the suite has since grown. Record the actual current count after running it rather than copying the old number.

## Remaining Work: Ordered Todo List

### Phase 0: Establish A Clean Baseline

- [ ] Run `git status --short` and inspect recent commits before editing.
- [ ] Run `python3 -m pytest` and record the actual result.
- [ ] Validate JSON/YAML syntax for `config/*.json` and `.github/workflows/daily-listings.yml`.
- [ ] Confirm the current schema applies cleanly to a disposable or current Supabase connection without destructive changes.
- [ ] Keep any real credentials out of command output and logs.

### Phase 1: Make Both Connectors Production-Ready

- [ ] Obtain/verify eBay developer access and a usable OAuth credential set.
- [ ] Verify the exact eBay API permissions and whether the refresh token supports `GetMyeBayBuying`.
- [ ] Enable the eBay source only after a real Browse API search succeeds.
- [ ] Run the configured Ralph Lauren search and verify returned data, price handling, image URLs, sale dates, and `max_price_usd: 100`.
- [ ] Decide whether account saved searches should replace or supplement committed searches; document the decision and preserve category/price metadata.
- [ ] Add/verify tests for eBay HTTP responses, API errors, pagination/limits, missing prices, malformed dates, and duplicate IDs.
- [ ] Confirm an empty or malformed Invaluable result produces an actionable parser-zero failure/alert rather than a silent empty digest.
- [ ] Ensure enrichment status remains separate from baseline listing fields and that repeated runs skip existing URLs safely.
- [ ] Decide and test behavior for missing prices or unavailable currency conversion. Do not accidentally accept an unknown price when a configured maximum must be enforced.

### Phase 2: Harden Deterministic Filtering

- [ ] Verify source/search configuration is preserved for dynamically loaded eBay saved searches and persisted in `raw_data`.
- [ ] Add tests for maximum price, excluded keywords, missing price, exact/normalized size matching, and disabled searches.
- [ ] Confirm waist size 28-30 is represented and screened correctly for the eBay clothing search, including title-only or unstructured size text.
- [ ] Decide whether deterministic filters should rerun all historical rows or only the current ingestion window.
- [ ] Keep `filter_status`, `filter_reason`, and `filtered_at` auditable and stable across reruns.

### Phase 3: Finish Portable Taste References

- [ ] Create/verify the private Supabase Storage bucket `taste-references`.
- [ ] Run `python -m listing_agent.cli upload-references --directory /Users/zach/Downloads/inspo_normalized` locally with storage credentials kept private.
- [ ] Verify all expected normalized images upload and all database rows reconcile to `storage_bucket`/`storage_path`.
- [ ] Retrieve signed URLs from a machine without the original Downloads tree and verify they expire as expected.
- [ ] Never send local filesystem paths to OpenAI or GitHub Actions.
- [ ] Store/use image dimensions and normalized hashes consistently; avoid duplicate reference rows.
- [ ] Keep `art`, `home_decor`, and `clothing` queries separate.
- [ ] Add support/tests for future active `dislike` reference rows without inventing dislikes now.
- [ ] Add descriptions only when grounded in user-provided information; blank descriptions are acceptable.
- [ ] Decide how to handle normalized files that have no existing metadata row and whether re-import should be required.

### Phase 4: Complete And Harden AI Judgment

- [ ] Verify the live OpenAI model identifier, availability, vision support, JSON response support, and current pricing. Do not assume `gpt-5.6-luna` is a valid public API model.
- [ ] Set the verified model consistently in `config/ai.json`, workflow secrets/config, and documentation.
- [ ] Validate the title-gate prompt and structured output against real eBay and generic Invaluable examples.
- [ ] Keep title/spec screening separate from visual taste judgment and batch title screening within safe request-size limits.
- [ ] Resize/compress listing and reference images before vision requests; enforce byte/count limits.
- [ ] Process only new listings or listings whose relevant image/title/spec input changed.
- [ ] Fix or verify cache hashing: signed URLs can change and should not invalidate an otherwise identical reference set; use stable storage paths/content hashes where appropriate.
- [ ] Store one concise reason and normalized verdict in `ai_judgments`; define retry/failure behavior so a failed AI call is not presented as a Like.
- [ ] Sample only a bounded, category-matched reference set rather than all references.
- [ ] Verify fast-track rules are intentionally limited, category-safe, and cannot bypass a required price/size filter.
- [ ] Verify `llm_usage` records both batched title usage and per-listing vision usage without double-counting retries unexpectedly.

### Phase 5: Digest And Feedback

- [ ] Run a real dry-run digest and inspect both plain text and HTML.
- [ ] Run a controlled SMTP delivery and verify inline images, links, USD/original pricing, source sections, sale timing, and concise reasons.
- [ ] Keep the digest suppressed when there are no passing listings; verify `include_filtered` is only a temporary validation setting.
- [ ] Verify digest idempotency and `digest_runs` behavior for repeated workflow runs on the same day.
- [ ] Keep item-specific Like/Dislike mailto subjects/bodies stable and parseable.
- [ ] Implement feedback IMAP ingestion: identify source, external ID, action, and title from replies; ignore malformed/unrelated mail safely.
- [ ] Insert feedback examples into the correct category with `like`/`dislike` labels and portable image references where available.
- [ ] Add feedback deduplication, audit fields, and tests. Do not use a public callback server unless the user changes the architecture decision.

## Current Configuration 

The workflow currently schedules at `0 12 * * *` UTC and supports manual dispatch. eBay and Invaluable are enabled in `config/searches.json`; eBay uses account saved searches with a global `max_price_usd: 10` cap, while Invaluable uses `zenrows` enrichment, category `art`, and `max_price_usd: 200`.

## Acceptance Criteria For “Complete”

- [ ] A real eBay Browse API run and a real Invaluable email run both produce normalized records in Supabase.
- [ ] Invaluable still produces usable baseline records when enrichment is challenged or unavailable.
- [ ] Currency conversion, deterministic price/size filtering, and audit fields are verified with real examples.
- [ ] Portable private reference images are retrievable from GitHub Actions and remain category-isolated.
- [ ] The verified OpenAI model performs title/spec and vision judgments with stable caching and persisted reasons.
- [ ] A scheduled run sends one correct consolidated digest only when qualifying listings exist.
- [ ] Like/Dislike replies are ingested into the correct category without duplicate or unsafe reference rows.
- [ ] Connector failures, parser-zero conditions, and delivery failures are visible and actionable.
- [ ] The full test suite passes and the workflow has completed at least one controlled manual end-to-end run.

## Known Risks

- Invaluable CloudFront/WAF behavior is intermittent. Email ingestion is the reliability baseline; enrichment is best effort.
- The exact `gpt-5.6-luna` API identifier is unverified and may need replacement.
- The current AI implementation creates a signed URL for each reference during hashing; this may cause unnecessary cache misses when URLs rotate.
- The current workflow exists but still lacks feedback processing and robust alert delivery.
