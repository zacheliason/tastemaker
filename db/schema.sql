create table if not exists listings (
  id bigserial primary key,
  source text not null,
  search_id text not null,
  external_id text not null,
  title text not null,
  price numeric(12, 2),
  currency text,
  price_usd numeric(12, 2),
  url text not null,
  image_urls jsonb not null default '[]'::jsonb,
  description text,
  size_fields jsonb not null default '{}'::jsonb,
  raw_data jsonb not null default '{}'::jsonb,
  filter_status text not null default 'pending',
  filter_reason text,
  filtered_at timestamptz,
  sale_end_at timestamptz,
  fetched_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  unique (source, external_id)
);

create index if not exists listings_search_fetched_idx on listings (search_id, fetched_at desc);

alter table listings add column if not exists price_usd numeric(12, 2);
alter table listings add column if not exists filter_status text not null default 'pending';
alter table listings add column if not exists filter_reason text;
alter table listings add column if not exists filtered_at timestamptz;
alter table listings add column if not exists sale_end_at timestamptz;

create table if not exists taste_references (
  id bigserial primary key,
  category text not null,
  label text not null check (label in ('like', 'dislike')),
  image_path text not null,
  image_sha256 text not null unique,
  mime_type text,
  source_image_path text,
  storage_bucket text,
  storage_path text,
  image_width integer,
  image_height integer,
  description text not null default '',
  active boolean not null default true,
  embedding jsonb,
  created_at timestamptz not null default now()
);

create index if not exists taste_references_category_idx on taste_references (category, label, active);

alter table taste_references add column if not exists source_image_path text;
alter table taste_references add column if not exists storage_bucket text;
alter table taste_references add column if not exists storage_path text;
alter table taste_references add column if not exists image_width integer;
alter table taste_references add column if not exists image_height integer;
alter table taste_references add column if not exists embedding jsonb;

create table if not exists ai_judgments (
  listing_id bigint primary key references listings(id) on delete cascade,
  model text not null,
  title_input_sha256 text not null,
  title_pass boolean,
  title_reason text,
  taste_input_sha256 text,
  taste_verdict text check (taste_verdict in ('like', 'dislike', 'uncertain')),
  taste_reason text,
  judged_at timestamptz not null default now()
);

create table if not exists digest_runs (
  digest_date date not null,
  recipient text not null,
  item_count integer not null,
  sent_at timestamptz not null default now(),
  primary key (digest_date, recipient)
);

create table if not exists llm_usage (
  id bigserial primary key,
  listing_id bigint references listings(id) on delete set null,
  operation text not null check (operation in ('title_gate', 'taste_judgment')),
  model text not null,
  prompt_tokens integer not null default 0,
  completion_tokens integer not null default 0,
  total_tokens integer not null default 0,
  listing_count integer not null default 1,
  recorded_at timestamptz not null default now()
);

create index if not exists llm_usage_recorded_at_idx on llm_usage (recorded_at desc);
