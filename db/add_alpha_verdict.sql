-- Alpha Verdict store (Supabase project pqzsdxcjyqjjvfsunzak).
--
-- One row per project: the Alpha Verdict (BUY/WATCH/SKIP + conviction), the 4 pillar scores, and
-- the "numbers at a glance". ask-alpha computes + writes this (porting the website's formula); it is
-- the shared CONTRACT the aredxb-next website will switch to reading so both surfaces agree.
--
-- Additive + RLS read policy (mirrors db/security_hardening.sql): the website's anon/service key can
-- SELECT; only the ask-alpha superuser writes. Idempotent; apply via Supabase SQL editor / MCP.

CREATE TABLE IF NOT EXISTS public.project_alpha_verdict (
    project_id   bigint PRIMARY KEY REFERENCES public.projects(id) ON DELETE CASCADE,
    conviction   numeric(6,2) NOT NULL,
    verdict      text NOT NULL CHECK (verdict IN ('BUY','WATCH','SKIP')),
    intent       text NOT NULL DEFAULT 'yield',
    -- pillar scores (0..100)
    yield_score  numeric(6,2),
    comp_score   numeric(6,2),
    thesis_score numeric(6,2),
    risk_score   numeric(6,2),
    -- numbers at a glance (percent-scaled where applicable)
    net_yield_pct           numeric,
    area_rent_return_pct    numeric,
    annual_appreciation_pct numeric,
    y5_value_aed            numeric,
    ppsf_aed                numeric,
    vs_area_price_pct       numeric,
    -- inputs / provenance
    community_slug  text,
    community_label text,
    used_fallback   boolean NOT NULL DEFAULT false,
    stats_source    text,                       -- 'property_monitor' | 'static_fallback'
    price_aed       numeric,
    beds            numeric,
    size_sqft       numeric,
    inputs          jsonb NOT NULL DEFAULT '{}'::jsonb,
    basis           text,
    formula_version text NOT NULL DEFAULT 'v1',
    stats_as_of     timestamptz,
    computed_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_pav_conviction ON public.project_alpha_verdict (conviction DESC);
CREATE INDEX IF NOT EXISTS ix_pav_verdict    ON public.project_alpha_verdict (verdict);
CREATE INDEX IF NOT EXISTS ix_pav_community  ON public.project_alpha_verdict (community_slug);

ALTER TABLE public.project_alpha_verdict ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_read_project_alpha_verdict ON public.project_alpha_verdict;
CREATE POLICY p_read_project_alpha_verdict ON public.project_alpha_verdict FOR SELECT USING (true);

-- Rollback:
--   DROP TABLE IF EXISTS public.project_alpha_verdict;
