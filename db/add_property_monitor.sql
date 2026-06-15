-- Property Monitor data layer (Supabase project pqzsdxcjyqjjvfsunzak).
--
-- Real per-community / per-project market data ingested from the PM AVM API. The verdict reads
-- pm_community_stats (yield/appreciation/ppsf) as the PRIMARY source, falling back to the static
-- model only when PM has no row. All raw payloads kept as jsonb for auditing / re-extraction.
--
-- Additive + RLS read policy (mirrors db/security_hardening.sql). Idempotent.

-- Our community -> PM location mapping.
CREATE TABLE IF NOT EXISTS public.pm_locations (
    community_slug    text PRIMARY KEY,
    pm_location_id    bigint,
    pm_location_name  text,
    emirate_id        int,
    area_name         text,
    master_development text,
    matched_query     text,
    raw               jsonb,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Derived per-community stats that FEED the verdict (the dynamic replacement for static COMMUNITY_DATA).
CREATE TABLE IF NOT EXISTS public.pm_community_stats (
    community_slug   text PRIMARY KEY,
    community_label  text,
    gross_yield      numeric,   -- decimal (e.g. 0.062)
    appreciation     numeric,   -- decimal YoY (e.g. 0.072)
    ppsf_aed         numeric,   -- indexed AED/sqft
    service_charge_aed_sqft numeric,
    sample_n         bigint,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Per-report AVM snapshot (preflight + consumer-avm preview). Keyed to a project when ingested
-- per-project, else to a community representative unit.
CREATE TABLE IF NOT EXISTS public.pm_reports (
    id                bigserial PRIMARY KEY,
    project_id        bigint REFERENCES public.projects(id) ON DELETE CASCADE,
    community_slug    text,
    pm_location_id    bigint,
    report_hash       text,
    report_id         bigint,
    bedrooms          text,
    unit_size_sqft    numeric,
    property_type_id  int,
    valuation_aed     numeric,
    valuation_low_aed numeric,
    valuation_high_aed numeric,
    ppsf_aed          numeric,
    service_charge_aed_sqft numeric,
    annual_service_charge_aed numeric,
    confidence_level  text,
    confidence_score  numeric,
    raw               jsonb,
    fetched_at        timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pm_reports_project ON public.pm_reports (project_id)
    WHERE project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_pm_reports_community ON public.pm_reports (community_slug);

-- Market trends (sales/rentals/yields), sold/transferred comps, active local-market activity,
-- lowest/highest comps, and the about-the-location narrative — per community, raw kept.
CREATE TABLE IF NOT EXISTS public.pm_market_trends (
    id bigserial PRIMARY KEY, community_slug text, report_hash text,
    raw jsonb, fetched_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pm_market_trends_comm ON public.pm_market_trends (community_slug);

CREATE TABLE IF NOT EXISTS public.pm_sold (
    id bigserial PRIMARY KEY, community_slug text, report_hash text,
    raw jsonb, fetched_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pm_sold_comm ON public.pm_sold (community_slug);

CREATE TABLE IF NOT EXISTS public.pm_local_activity (
    id bigserial PRIMARY KEY, community_slug text, report_hash text,
    raw jsonb, fetched_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pm_local_activity_comm ON public.pm_local_activity (community_slug);

CREATE TABLE IF NOT EXISTS public.pm_lowest_highest (
    id bigserial PRIMARY KEY, community_slug text, report_hash text,
    raw jsonb, fetched_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pm_lowest_highest_comm ON public.pm_lowest_highest (community_slug);

CREATE TABLE IF NOT EXISTS public.pm_about_location (
    community_slug text PRIMARY KEY, report_hash text,
    raw jsonb, fetched_at timestamptz NOT NULL DEFAULT now()
);

-- RLS: permissive read, no write policy (superuser app writes).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['pm_locations','pm_community_stats','pm_reports','pm_market_trends',
                           'pm_sold','pm_local_activity','pm_lowest_highest','pm_about_location']
  LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS p_read_%I ON public.%I', t, t);
    EXECUTE format('CREATE POLICY p_read_%I ON public.%I FOR SELECT USING (true)', t, t);
  END LOOP;
END $$;

-- Rollback:
--   DROP TABLE IF EXISTS public.pm_locations, public.pm_community_stats, public.pm_reports,
--     public.pm_market_trends, public.pm_sold, public.pm_local_activity,
--     public.pm_lowest_highest, public.pm_about_location CASCADE;
