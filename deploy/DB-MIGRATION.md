# Moving the ask-alpha DB Closer to UAE — The Honest Runbook

_Researched & adversarially verified 2026-06-22. Audience: a developer who knows the app but is not an AWS/DB specialist._

---

## 1. The honest answer

**You cannot move the Supabase database to the UAE.** Supabase has no Middle East region. The live Supabase provisioning API offers exactly 16 AWS regions, and `me-central-1` (UAE), `me-south-1` (Bahrain), and `il-central-1` (Israel) are **not** among them. A community request to add a Middle East region (GitHub Discussion #34551, opened 2025-03-30) is still unanswered, so none is imminent. (Sources: https://supabase.com/docs/guides/platform/regions , https://github.com/orgs/supabase/discussions/34551)

Three more facts decide the whole plan:

- **Your DB is already in the closest Supabase region to Dubai.** Mumbai (`ap-south-1`) is ~1,930 km from Dubai (~35 ms, which matches your measured number). The next-nearest Supabase regions — Frankfurt/Zurich (~4,850 km) and Singapore (~5,800 km) — are all **2.5x+ farther**. Moving to any "other" Supabase region makes Dubai latency **worse**, not better.
- **Do NOT move the app to AWS `me-central-1` while the DB stays on Supabase-Mumbai.** That moves the app _away_ from the DB and increases app↔DB latency. If you stay on Supabase, the app should sit in/near **Mumbai (`ap-south-1`)**, co-located with the DB.
- **A Supabase read replica does not help.** Replicas can only live in Supabase's existing regions, and the closest is the one you're already in (Mumbai). Replicas buy HA / read-scaling, not UAE latency. (Source: https://supabase.com/docs/guides/platform/read-replicas)

Your real problem is `turn_latency ≈ N_sequential_roundtrips × RTT`. A region move can only attack `RTT`, and Supabase can't get `RTT` below the Mumbai floor for Dubai. So the highest-value lever is **cutting `N`** — the number of sequential DB round-trips per chat turn — which requires **no migration at all**.

### Ranked realistic options

| Rank | Option | What it does | Pros | Cons |
|---|---|---|---|---|
| **A (do first)** | **Keep Supabase in Mumbai + cut round-trips at the app layer** (kill N+1, batch/parallelize, drop `pool_pre_ping`) | Attacks `N` in `N×RTT` | Zero infra change, zero auth/RLS risk, reversible, ships this week, **compounds** with any later move | Bounded by how many trips you can actually remove; can't beat the serial chain you truly need |
| **A2 (cheap complement)** | **Co-locate the app in `ap-south-1` (Mumbai), not UAE** | Keeps app next to the DB | Small RTT win, no DB migration, keeps Supabase Auth/RLS intact | App is now far from UAE users for non-DB work (usually negligible — the bottleneck is DB round-trips, not the single user↔app hop) |
| **B (only for HA)** | New Supabase project in another region / read replica | Region change / read-scaling | Managed; keeps Auth + RLS | **No region beats Mumbai for Dubai**; new project = new ref → key/URL/JWKS churn + likely forced re-login (see §3); replica lag; does not reduce `N`. **Does not achieve the UAE goal.** |
| **C (the only true UAE co-location)** | **Self-managed Postgres / RDS in `me-central-1`, keep Supabase only for Auth** | App + data both in UAE (~1 ms RTT) | Genuinely fixes RTT; pgvector supported on RDS 15.2+; login UX preserved (JWKS verification survives the split) | **Major re-architecture**: cross-DB FKs to `auth.users` become impossible; RLS `auth.uid()` helpers break for the frontend; you must build a user→profile sync; you now operate a DB. (Sources: codebase `app/db/models.py`, `db/security_hardening.sql`; https://supabase.com/docs/guides/database/postgres/row-level-security ; PostgreSQL has no cross-DB FKs) |

### Recommendation

**Do Option A now.** It is the largest realistic win for the least risk, it ships immediately, and it makes any future co-location pay off even more (e.g. ~6x now from cutting trips, then up to ~35x per remaining trip if you ever co-locate). The runbook in §2 is for Option A.

Only **after** A is done, decide whether you still need lower RTT. If you do, the genuine UAE co-location is **Option C** (data in `me-central-1`, Auth left on Supabase), with the caveats in §3. A cleaner variant of C, if you want to avoid the two-DB split entirely, is to **self-host GoTrue (Supabase Auth) against the same `me-central-1` Postgres** so `auth.users` and your data stay co-located and FKs survive — at the cost of operating GoTrue yourself. (Source: https://supabase.com/docs/reference/self-hosting-auth/introduction)

---

## 2. Runbook for the recommended option (A): cut round-trips, no migration

This is safe, reversible, and touches only `app/db/session.py` plus query code. Work on a branch.

### Step 1 — Branch and baseline-measure first (so you can prove the win)

```bash
git checkout -b perf/cut-db-roundtrips
```

Add a one-line timing log around a chat turn (or use your existing logging). The numbers you care about are **per-turn DB time** and **count of DB round-trips per turn**. Capture a "before" number for 3–5 representative chat turns; you'll compare against it in the verify step.

### Step 2 — Count the round-trips in one chat turn (the diagnosis)

Temporarily turn on SQLAlchemy SQL echo to literally see every query a single turn fires:

```python
# in app/db/session.py, set echo=True TEMPORARILY (revert before commit)
```

Then run one chat turn and count the `SELECT`/`INSERT` lines in the log. Each line is one ~35 ms Mumbai round-trip if it runs sequentially. You are hunting for three patterns:

1. **N+1 queries** — a list fetched, then one extra query _per row_ (e.g. fetch projects, then per-project fetch assets/units/POIs/alpha verdict). Look in the chat tool path:
   - `app/tools/` (e.g. `pois.py`, `market.py`, `documents.py`, `social.py`)
   - `app/rag/agentic.py` (RAG retrieval)
   - any project/unit/asset list builder that then loops and queries per item (the "conviction-first" list surfaces are prime suspects since they enrich each card).
2. **Independent queries run serially** that could run concurrently (e.g. fetch documents AND market transactions AND POIs for the same turn, one after another).
3. **Per-turn lookups of static reference data** (district lists, `investment_yield_assumptions`, developer metadata) that rarely change.

### Step 3 — Fix N+1 with eager loading

For ORM relationships, load children in one round-trip instead of one-per-parent:

```python
from sqlalchemy.orm import selectinload, joinedload

stmt = (
    select(Project)
    .options(
        selectinload(Project.units),
        selectinload(Project.assets),
        selectinload(Project.alpha_verdict),
    )
    .where(...)
)
```

- `selectinload` → one extra query total per relationship (1 + a few), not 1-per-row. Best default for collections.
- `joinedload` → folds a to-one relationship into the same query (zero extra round-trips). Best for single related rows.

For **raw-SQL** N+1 (e.g. `app/tools/pois.py`, `app/reports/market_data.py` looping over `pm_*` tables), rewrite the per-row queries as a single query using `WHERE id = ANY(:ids)` / a `JOIN` / a CTE so one trip returns everything.

### Step 4 — Parallelize the truly-independent queries

If a turn needs several **unrelated** result sets, run them concurrently on separate sessions instead of serially:

```python
import asyncio
from app.db.session import AsyncSessionLocal

async def _q(coro_fn):
    async with AsyncSessionLocal() as s:
        return await coro_fn(s)

docs, txns, pois = await asyncio.gather(
    _q(fetch_documents),
    _q(fetch_market_transactions),
    _q(fetch_pois),
)
```

Three serial 35 ms trips (~105 ms) collapse toward one (~35 ms). Use this only for queries that don't depend on each other's output. It consumes more pool connections at once — keep an eye on pool sizing (Step 5).

### Step 5 — Remove the per-checkout `pool_pre_ping` round-trip

`pool_pre_ping=True` emits a `SELECT 1` **every time a connection is checked out** — a full extra ~35 ms RTT on the first DB op of a turn. Replace it with `pool_recycle` plus SQLAlchemy's built-in disconnect handling. (Source: https://docs.sqlalchemy.org/en/20/core/pooling.html)

Edit `app/db/session.py`:

```python
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=False,        # was True: removed one SELECT 1 RTT per checkout
    pool_recycle=1800,          # recycle connections older than 30 min (below Supavisor idle timeout)
)
```

> If you see occasional stale-connection errors after this (Supavisor closing idle conns), lower `pool_recycle` (e.g. 600) rather than re-enabling `pool_pre_ping`. Keep the pool warm so you don't pay TCP+TLS setup (several RTTs) on cold connections.

**Do NOT change `DB_PORT`.** Stay on the session pooler (`5432`). The transaction pooler (`6543`) breaks asyncpg's prepared statements.

### Step 6 — Cache static reference data (optional, high value if present)

For lookup data that changes rarely (`investment_yield_assumptions`, district lists, developer metadata), cache it in-process with a short TTL so it leaves the hot path entirely. Even a simple module-level dict with a timestamp removes those trips from every turn.

### Step 7 — No changes needed to `config.py` or `auth.py`

Option A does not move the DB or the project, so **`app/config.py` (`database_url`, `DB_*`, `supabase_url`) and `app/core/auth.py` (JWKS verification) stay exactly as they are.** No env, secret, ref, or JWKS changes. This is precisely why A is low-risk.

### Step 8 — No Vercel frontend changes

The frontend keeps the same Supabase URL + anon key. Nothing to redeploy on Vercel for Option A.

### Data migration steps

**None.** Option A does not touch data, pgvector, or `auth.users`. (Migration steps only apply to Options B/C — see §3 for why those are heavier.)

### Verify the latency improved

1. Re-run the same 3–5 chat turns you baselined in Step 1.
2. Compare **per-turn DB time** and **round-trip count** before/after. Expect the count to drop sharply (e.g. 30 → 5) and per-turn DB time to fall roughly proportionally.
3. Sanity-check correctness, not just speed: confirm the enriched cards (units/assets/alpha verdict/POIs) still render fully — eager loading must not drop data.
4. Watch logs for stale-connection errors after dropping `pool_pre_ping`; tune `pool_recycle` if any appear.
5. Turn `echo` back to `False` before committing.

### Rollback plan

Everything is in one branch and is config/query-level:

```bash
git checkout main          # abandon the branch entirely, OR
git revert <commit>        # undo a specific change after merge
```

Re-set `pool_pre_ping=True` and remove `pool_recycle` to restore the old pool behavior; eager-loading and `asyncio.gather` changes are self-contained per query and can be reverted individually. No data or auth state is affected, so rollback is instant and risk-free.

---

## 3. What NOT to do / gotchas

These apply mainly if someone pushes for Option B (new project / region) or C (UAE self-managed). Read before agreeing to either.

- **Don't move the app to UAE while the DB stays on Supabase-Mumbai.** It increases app↔DB latency. Co-locate the app in Mumbai instead. (Verified: Supabase has no UAE region.)
- **Don't expect any Supabase region to beat Mumbai for Dubai.** Frankfurt/Zurich/Singapore are all 2.5x+ farther. A region "move" within Supabase cannot achieve the UAE goal and will likely make latency worse.
- **A read replica won't help UAE latency.** Closest replica region = Mumbai = where you already are. And replicas are **async** — any read that must reflect a just-written row (write a chat message, then read the thread) would risk stale data and must be pinned to the primary. (Source: https://supabase.com/blog/introducing-read-replicas)
- **New Supabase project = new project ref = cascading changes.** It changes the project URL, the anon/service-role keys, AND the JWKS endpoint. You'd have to update the **Vercel frontend** (`NEXT_PUBLIC_SUPABASE_URL` + anon key) and the **backend** `SUPABASE_URL` (which derives the JWKS URL in `app/core/auth.py:53`), in the right order: bring up the new project → switch the frontend to issue new-project tokens → only then flip the backend `SUPABASE_URL`. Flip the backend too early and it rejects every token. **Restart all processes** afterward to drop the cached `PyJWKClient` (it caches keys for 600 s and won't switch hosts on its own). (Sources: https://supabase.com/docs/guides/troubleshooting/change-project-region-eWJo5Z ; `app/core/auth.py`)
- **Session invalidation is likely-but-not-absolute, and for THIS app it's effectively forced.** Reusing the legacy HS256 JWT secret could preserve sessions in general — but ask-alpha verifies **asymmetric ES256** tokens against the JWKS with no shared secret (`app/core/auth.py:9-10,37-39`). For asymmetric keys, Supabase cannot export the private key it generated for you, so unless you originally imported your own ES256 key, a new project means a **one-time forced re-login**. That's acceptable (passwords/identities migrate intact — users just sign in again), but plan for it. (Source: https://supabase.com/docs/guides/auth/signing-keys)
- **RLS / roles must be reproduced on any new DB.** The backend connects as the `postgres` role with **BYPASSRLS** and reads RLS tables directly (`db/security_hardening.sql`). On any migrated/self-managed DB, the connection role must keep BYPASSRLS or app reads silently return zero rows. The **frontend's** anon-key reads depend on the `USING(true)` RLS policies — those must be re-applied too.
- **Cross-DB FKs are impossible (Option C).** If `auth.users` stays on Supabase and your data tables move to `me-central-1`, the FKs `profiles.id == auth.users.id` and `heygen_avatars.user_id → auth.users(id)` (`app/db/models.py:173,194-196`) cannot exist across two servers. You'd drop them, keep `user_id` as plain UUIDs, lose `ON DELETE CASCADE`, and build a user→profile **sync pipeline** (the `auth.users` signup trigger can't write into RDS). This is the "major re-architecture" cost.
- **pgvector must be pre-enabled before any schema restore (Options B/C).** `CREATE EXTENSION vector;` must run on the target _before_ restoring `document_chunks` (dimension 1024). Vector data migrates as ordinary COPY data; if the index is IVFFlat, build it _after_ loading data. (Source: https://supabase.com/docs/guides/database/extensions/pgvector)
- **Downtime / cutover (Options B/C).** Supabase can't change region in place — you create a new project and migrate via `pg_dump`/`psql --single-transaction` (with `SET session_replication_role = replica` during data load). There's a write-freeze cutover window. The CLI `db dump` **excludes** the `auth` and `storage` schemas by default, so migrating `auth.users` requires an explicit `pg_dump --schema=auth` (preserving UUIDs) or the dashboard physical `.backup`. For ask-alpha, Supabase Storage is **not** used (assets are in your own eu-west-2 S3), so storage migration is skippable — but confirm `storage.objects` is empty first. (Sources: https://supabase.com/docs/guides/platform/migrating-within-supabase/backup-restore ; https://supabase.com/docs/guides/troubleshooting/migrating-auth-users-between-projects)

---

## 4. Where to look in THIS codebase for round-trip wins (the biggest realistic lever)

Concrete targets, since Option A is the recommendation:

- **`app/db/session.py:10`** — `pool_pre_ping=True`. One guaranteed extra `SELECT 1` (~35 ms) per connection checkout. Replace with `pool_recycle` (Step 5). Single highest-certainty fix.
- **`app/rag/agentic.py`** — RAG retrieval per chat turn (pgvector similarity ~lines 43–53). Make sure embedding lookup + chunk fetch + any per-chunk enrichment isn't a serial chain; batch it.
- **`app/tools/pois.py`** (raw SQL ~25,37,62,70) — classic N+1 if POIs are fetched per project in a loop. Collapse to one `WHERE project_id = ANY(:ids)`.
- **`app/tools/market.py`** and **`app/reports/market_data.py`** — multiple `pm_*` / `market_transactions` / `investment_yield_assumptions` reads. Check whether they run serially when they could `asyncio.gather`, and cache `investment_yield_assumptions` (rarely changes).
- **`app/tools/documents.py`** — vector search + chunk fetch; ensure it's one retrieval, not retrieve-then-loop.
- **The conviction-first list surfaces** (project/property lists that put a score + price + assets on every card) — these enrich each row and are the most likely N+1 hot spot. Use `selectinload`/`joinedload` on `Project.units`, `Project.assets`, `Project.alpha_verdict` so a list of N projects is a few queries, not N×(2–4).
- **Static lookups** (`investment_yield_assumptions`, district lists, developer metadata) — cache in-process with a TTL so they leave the per-turn hot path entirely.

Method: set `echo=True` in `app/db/session.py`, run one chat turn, count the SQL lines. Every sequential line is ~35 ms today. Cutting 30 serial trips to ~5 is roughly a **6x per-turn latency cut with zero infrastructure change** — and it compounds with any later co-location. (Sources: https://docs.sqlalchemy.org/en/20/core/pooling.html ; https://neon.com/blog/how-to-minimise-the-impact-of-database-latency)
