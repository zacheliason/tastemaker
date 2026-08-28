# Daily Listing Taste-Filter Agent Handoff

This document is the source of truth for continuing work in a fresh LLM conversation. Read it before changing code. The project lives at `/Users/zach/Documents/ow`.

## User decisions

- Ingestion uses email alerts for Invaluable and the official eBay Browse API. Do not scrape eBay pages.
- Hosting and scheduling will be GitHub Actions, represented as version-controlled workflow YAML.
- State is hosted Postgres, currently Supabase.
- OpenAI is used for vision/taste judgment. The user refers to the lightweight title/spec model as `gpt-5.6 Luna`; verify the exact currently available model/API identifier before implementation.
- All secrets belong in GitHub Actions secrets or local `.env`; never commit or expose them.
- The initial sources are eBay clothing and Invaluable listings.
- eBay test search: `ralph lauren polo cream pleated trousers waist size 30`.
- eBay deterministic maximum price: `$100`, represented consistently as `max_price_usd: 100`.
- eBay title/spec matching should use a lightweight model before vision taste judgment, preferably batched to reduce token use.
- Invaluable currently accepts all listing-like items found in the dedicated inbox. The original artist/woodblock/$300 test filter was removed.
- Invaluable and eBay must use a shared normalized schema and modular source adapters.
- Keep taste reference pools separate by category: `art`, `home_decor`, and `clothing`. Use one shared judging interface, but do not compare art references against clothing or decor references.
- Pinterest references: import all available positive examples. Dislikes can be added later through feedback.
- Invaluable page enrichment is wanted and should remain in the plan, but it must not make baseline email ingestion unreliable.
- Final user-facing output should be a consolidated daily email digest. Feedback should ideally use pre-addressed Like/Dislike email replies because GitHub Actions cannot receive web clicks without another hosted endpoint.

## Current repository

- `listing_agent/ebay.py`: eBay OAuth client-credentials token and Browse API search.
- `listing_agent/invaluable.py`: IMAP ingestion, Invaluable email parser, JSON-LD page parser, Playwright browser enrichment, four attempts with exponential backoff.
- `listing_agent/pricing.py`: currency string parsing and FreeCurrencyAPI conversion to USD.
- `listing_agent/models.py`: normalized `Listing` dataclass with original `price`, `currency`, and `price_usd`.
- `listing_agent/db.py`: idempotent Postgres upserts.
- `listing_agent/filters.py`: configurable deterministic filters and audit status updates.
- `listing_agent/references.py`: imports local reference image metadata into Postgres.
- `listing_agent/cli.py`: commands `ingest`, `filter`, `import-references`, and `enrich-url`.
- `scripts/normalize_images.py`: normalizes local reference images to JPEG.
- `db/schema.sql`: listings, filtering fields, and `taste_references` table.
- `config/searches.json`: source/search configuration.
- `tests/`: parser and pricing tests.

## Environment

`.env` exists locally and is gitignored. Do not read, print, or paste its values. Required variables now include:

```text
DATABASE_URL
EBAY_CLIENT_ID
EBAY_CLIENT_SECRET
EBAY_MARKETPLACE_ID
IMAP_HOST
IMAP_PORT
IMAP_USERNAME
IMAP_PASSWORD
IMAP_FOLDER
FREECURRENCYAPI_KEY
```

`FREECURRENCYAPI_KEY` is required whenever a non-USD price must be converted. The current Invaluable Josef Albers page reports EUR, so a full enriched ingestion will need this key.

## Verified real work

### Invaluable IMAP

- Gmail IMAP connection succeeds.
- A manually forwarded email titled `Fwd: New pieces from Josef Albers and Sabra Field added` was valid.
- The email parser was corrected for Invaluable's actual HTML: listing images use `alt="lot image"`, while titles are in separate table cells.
- The current email-only ingestion fetched and upserted 3 real Invaluable listings into Supabase:
  - Josef Albers
  - Sabra Field woodblock triptych
  - Sabra Johnson Field woodblock
- Page email data did not include prices.

### Supabase

- Supabase Session Pooler connection works.
- Database schema and additive migrations were applied manually through psycopg because `psql` is not installed locally.
- `listings` had 3 real Invaluable rows before Phase 2.

### Phase 2 filters

Real run:

```text
invaluable: before=3 passed=3 filtered=0
```

All passed because Invaluable currently has no configured deterministic rules. Each row has `filter_status`, `filter_reason`, and `filtered_at`.

### Browser page enrichment

The exact Josef Albers URL was successfully loaded once through Playwright and parsed from page-source JSON-LD:

```text
title: Josef Albers (1888 Bottrop - New Haven 1976)
price: 2000 EUR
external_id: e978405423
images: 1
```

Ordinary `httpx` received an Invaluable CloudFront WAF challenge. Playwright was intermittent: one run succeeded, later runs returned `JavaScript is disabled`. The browser parser now detects challenge pages and does not treat them as valid listing data.

### Pinterest references

The original `/Users/zach/Downloads/inspo` folders contained:

- `pinterest_art`
- `pinterest_clothes`
- `pinterest_home_decor`

The importer stored 316 unique positive reference metadata rows in Supabase:

```text
art: 269
clothing: 23
home_decor: 24
total: 316
```

The image files were not copied into the repository or database. Their database paths point to local Downloads paths.

The normalizer was run successfully:

```text
input:  /Users/zach/Downloads/inspo
output: /Users/zach/Downloads/inspo_normalized
normalized: 338
failures: 0
output size: 47 MB
all output files: JPEG
maximum dimension: 1600px
```

There are now 338 files in the normalized output because the Downloads folder contents changed after the initial import. The database still points at original files and must be reconciled with normalized storage.

## Test status

Current local tests:

```text
4 passed
```

Run with:

```sh
python3 -m pytest
```

## Important incomplete items

1. eBay developer access is still pending. Do not block all Invaluable work on it.
2. `FREECURRENCYAPI_KEY` is not currently configured in the local `.env`.
3. Invaluable browser enrichment is active in the fetch path but not yet proven stable across repeated runs. It retries four times and falls back to email data.
4. The reference image metadata is in Postgres, but the actual images are only local files. GitHub Actions cannot use those paths.
5. Reference examples have blank descriptions and are all labeled `like`; no dislikes have been collected.
6. No OpenAI title gate or taste judgment implementation exists yet.
7. No digest email sender exists yet.
8. No feedback ingestion exists yet.
9. No GitHub Actions workflow exists yet.
10. No failure alerting or parser-zero alerting exists yet.

## Recommended next sequence

### A. Make references portable

Recommended storage is a private Supabase Storage bucket because the normalized set is approximately 47 MB and should not bloat the Git repository. Add the required Supabase storage credential only as a secret. Upload `/Users/zach/Downloads/inspo_normalized`, update `taste_references` to portable storage paths, and handle private signed URLs. Convert/verify the HEIC input through the normalizer, which currently succeeded using Pillow plus pillow-heif.

Do not begin Phase 4 until reference images can be retrieved outside this machine.

### B. Finish Phase 3

Add descriptions only where useful; do not invent user preferences. Keep category-specific reference retrieval. Consider storing image dimensions, normalized hash, and storage path. Add a mechanism for future `dislike` rows.

### C. Implement Phase 4

Before sending images to OpenAI:

- Process only new listings or changed images.
- Cache judgments by listing/image hash.
- Resize/compress images.
- Use category-specific reference samples rather than all 316 images in each prompt.
- Use the selected current cheapest vision-capable OpenAI model after checking live pricing/model availability.
- Store verdict and one-sentence reasoning in Postgres.

The title/spec gate should be a separate interface and batch requests where supported. It should check configured search settings and structured/recognized size data, especially waist size 30 for eBay.

### D. Phase 5

Build a daily email digest with sections by source. Include image, title, USD price, original price/currency, URL, and reason. Use `mailto:` links with item-specific feedback subjects/bodies rather than adding a public web server. Parse feedback replies and add labeled examples to the proper category pool.

### E. Phase 6

Add `.github/workflows/daily-listings.yml` as IaC. It should install dependencies and Playwright Chromium, run migrations safely, run ingestion, filters, title gate, taste judgment, feedback processing, and digest delivery. Make parser-zero and connector failures fail loudly and alert.

## Cautions

- Do not move to claiming full Phase 1 completion until a real eBay API run and real Invaluable records are both verified, although Invaluable-first work is explicitly allowed.
- Do not expose `.env`; it contains credentials.
- Do not assume Invaluable browser enrichment will work on every GitHub Actions run. Keep email baseline and enrichment status separate.
- Do not call Pinterest or Invaluable private endpoints without checking permissions/terms.
- Do not mix art, home decor, and clothing taste references in one classifier prompt unless there is a deliberate shared-base plus category-specific design.
