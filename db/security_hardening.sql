-- Phase 7 — Security hardening for Ask Alpha (Supabase project pqzsdxcjyqjjvfsunzak).
--
-- NOT applied automatically: enabling RLS / altering policies changes access on the
-- shared production database, so apply this deliberately yourself (Supabase SQL editor
-- or `supabase db push`) after confirming your frontend does NOT read these tables via
-- the anon key. The Ask Alpha backend connects as the postgres superuser, which bypasses
-- RLS, so the app itself is unaffected by these changes.
--
-- Addresses Supabase advisor findings:
--   ERROR rls_disabled_in_public: market_transactions, document_chunks, videos, messaging_links
--   WARN  function_search_path_mutable: get_market_sentiment, touch_updated_at
--   (The 4x security_definer_view findings on v_market_sentiment_* are intentionally
--    left out here — switching them to security_invoker can change anon read behavior;
--    review separately.)

-- 1) Enable RLS + permissive read on reference/data tables (reads preserved, anon
--    writes blocked because there is no write policy).
ALTER TABLE public.market_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.document_chunks     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.videos              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.messaging_links     ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS p_read_market_transactions ON public.market_transactions;
CREATE POLICY p_read_market_transactions ON public.market_transactions FOR SELECT USING (true);

DROP POLICY IF EXISTS p_read_document_chunks ON public.document_chunks;
CREATE POLICY p_read_document_chunks ON public.document_chunks FOR SELECT USING (true);

DROP POLICY IF EXISTS p_read_videos ON public.videos;
CREATE POLICY p_read_videos ON public.videos FOR SELECT USING (true);

-- messaging_links is sensitive (profile <-> channel identity): no anon read policy.
-- RLS-enabled with no policy blocks anon entirely; the superuser app connection is fine.

-- 2) Pin mutable function search paths (keeps the functions working).
ALTER FUNCTION public.get_market_sentiment(text) SET search_path = public, pg_catalog;
ALTER FUNCTION public.touch_updated_at()         SET search_path = public, pg_catalog;

-- Rollback (if needed):
--   ALTER TABLE public.market_transactions DISABLE ROW LEVEL SECURITY;  -- etc.
--   DROP POLICY p_read_market_transactions ON public.market_transactions;  -- etc.
