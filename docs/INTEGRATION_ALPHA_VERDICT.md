# Alpha Verdict + Property Monitor — Integration Guide

ask-alpha now computes the **Alpha Verdict** (BUY/WATCH/SKIP + conviction + 4 pillars + "numbers
at a glance") for every priceable project and stores it in the shared **Projects DB** (Supabase
`pqzsdxcjyqjjvfsunzak`). Numbers are backed by **real Property Monitor** data where available
(per-community ppsf + appreciation, per-project AVM valuations), with a static area model as
fallback. This guide is for two consumers:

1. **The website (`aredxb-next`)** — read the verdict from the DB instead of computing it client-side.
2. **The Alpha chat web frontend** — render the new chat cards.

---

## 1) Website (`aredxb-next`) — read the shared verdict

**Replace** the client-side `lib/analyzers.ts` / `lib/quickVerdict.ts` computation with a read of
the `project_alpha_verdict` table. ask-alpha is the single source of truth; both apps then show
identical numbers.

### Table: `public.project_alpha_verdict` (one row per project)
RLS is enabled with a permissive **SELECT** policy, so your existing Supabase anon/service key can
read it (no writes — only ask-alpha writes).

| column | meaning |
|---|---|
| `project_id` (PK → projects.id) | the project |
| `verdict` | `'BUY' \| 'WATCH' \| 'SKIP'` |
| `conviction` | 0–100 (display rounded, e.g. 75) |
| `yield_score`, `comp_score`, `thesis_score`, `risk_score` | the 4 pillars 0–100 (Yield vs Community, Price/sqft vs Community, Yield vs Dubai, Risk & Safety) |
| `net_yield_pct`, `area_rent_return_pct`, `annual_appreciation_pct`, `y5_value_aed`, `ppsf_aed`, `vs_area_price_pct` | the "Numbers at a glance" |
| `community_slug`, `community_label` | resolved community |
| `stats_source` | `'property_monitor'` (real) \| `'static_model'` \| `'static_fallback'` |
| `used_fallback` | true when the community isn't in our market model (area estimate) |
| `basis` | one-line disclaimer string to show under the numbers |
| `formula_version`, `computed_at`, `stats_as_of` | provenance/freshness |

### Read it (supabase-js, mirrors your `lib/supabase.ts`)
```ts
const { data } = await supabase
  .from('project_alpha_verdict')
  .select('verdict, conviction, yield_score, comp_score, thesis_score, risk_score, '
        + 'net_yield_pct, area_rent_return_pct, annual_appreciation_pct, y5_value_aed, '
        + 'ppsf_aed, vs_area_price_pct, community_label, stats_source, used_fallback, basis')
  .eq('project_id', projectId)
  .maybeSingle();
```
Or REST: `GET {SUPABASE_URL}/rest/v1/project_alpha_verdict?project_id=eq.{id}&select=*` (with `apikey`).

- The **project page** "ALPHA VERDICT" badge = `verdict` + `conviction`.
- The **"Is this a good buy?" pillars** = `yield_score / comp_score / thesis_score / risk_score`.
- The **"Numbers at a glance"** = the six number columns above.
- If a project has **no row**, it has no priced units yet — keep your current empty/placeholder state.
- Keep `canonicalCommunitySlug()` consistent with ours (marina→dubai-marina, jvc→jvc, …); we store `community_slug` so you can just read it.

### Optional — live Property Monitor data (`pm_*` tables, also RLS-readable)
- `pm_community_stats` (per `community_slug`): `ppsf_aed`, `appreciation`, `service_charge_aed_sqft`.
- `pm_reports` (per `project_id` when present, else community-level): `valuation_aed`, `valuation_low_aed`, `valuation_high_aed`, `ppsf_aed`, `confidence_level`, `fetched_at`.
- `pm_sold`, `pm_market_trends`, `pm_local_activity`, `pm_lowest_highest`, `pm_about_location`: raw PM payloads (`raw jsonb`) per community.

> Note: PM ppsf/valuation are real per project/community; appreciation currently reads ~Dubai-wide; PM doesn't expose rental yield via these endpoints, so `area_rent_return_pct`/`net_yield_pct` use our model.

---

## 2) Alpha chat web frontend — render the new cards

The chat API is unchanged in shape: `POST /v1/chat` → `{ reply, cards: [...] }`. Three card
additions to render (ignore-if-unknown is safe):

### `project_list` (search results) — now carries the verdict
Each item now includes `verdict` and `conviction`. **Render results in the order returned** (they're
already ranked by Alpha conviction, highest first) and show a badge per card.
```jsonc
{ "type": "project_list", "has_more": true, "next_offset": 5,
  "items": [ { "id": 2291, "name": "105 Residences", "developer": "Kamdar",
               "city": "Dubai", "district": "JVC (Jumeirah Village Circle)",
               "min_price": 755000, "currency": "AED",
               "verdict": "WATCH", "conviction": 58 /* may be null */ } ] }
```

### `alpha_verdict` (a single project's verdict — "is X a good buy")
```jsonc
{ "type": "alpha_verdict", "project_id": 2291, "project_name": "105 Residences",
  "verdict": "WATCH", "conviction": 75,
  "pillars": { "yield": 94, "comp": 13, "thesis": 97, "risk": 55 },
  "numbers": { "net_yield_pct": 7.6, "area_rent_return_pct": 8.2,
               "annual_appreciation_pct": 8.5, "y5_value_aed": 1140000,
               "ppsf_aed": 1728, "vs_area_price_pct": 57.1 },
  "community": "Jumeirah Village Circle", "used_fallback": false, "basis": "…" }
```
Suggested UI: verdict badge + conviction ring, the 4 pillar bars, the numbers row; show `basis` small.

### `live_market` (Property Monitor live data)
```jsonc
{ "type": "live_market", "project_id": 2291, "project_name": "105 Residences",
  "community": "Jumeirah Village Circle", "valuation": 2721500,
  "ppsf_aed": 1356, "observed_yield_pct": null, "sold": [ /* recent comps */ ],
  "fetched_at": "2026-06-15T..." }
```
Label this clearly as **Property Monitor (live)** — distinct from the Alpha Verdict (our model).

### Request contract (unchanged)
```jsonc
POST /v1/chat
{ "message": "is 105 Residences a good buy?", "user_id": "<uuid>", "channel": "website",
  "conversation_id": "<uuid|null>" }
```
`reply` is the prose to show; `cards` carry the structured detail — don't re-sort `project_list`.

---

## Freshness / ops
- The verdict store recomputes on read if older than `alpha_verdict_max_age_days` (7) or on a new
  `formula_version`; a full refresh runs via `python -m app.analytics.verdict_backfill`.
- PM data is refreshed by `python -m app.ingest.property_monitor_ingest` (communities) and
  `… property_monitor_ingest projects` (per-project AVMs). The runner host's IP must be
  Property-Monitor-allowlisted, and PM's API requires **HTTP/2**.
