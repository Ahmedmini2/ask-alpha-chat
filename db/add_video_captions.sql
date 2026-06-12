-- Caption post-processing columns for the videos table (Supabase project pqzsdxcjyqjjvfsunzak).
--
-- Additive + nullable, so safe to apply on the shared production DB and trivially
-- reversible (DROP COLUMN). Run in the Supabase SQL editor, via `supabase db push`,
-- or the Supabase MCP apply_migration.
--
--   captioned_video_url : presigned S3 URL of the Hormozi-captioned MP4 (generated/videos/*)
--   caption_status      : NULL | 'processing' | 'done' | 'failed'
--   caption_error       : failure detail when caption_status = 'failed'

ALTER TABLE public.videos
  ADD COLUMN IF NOT EXISTS captioned_video_url TEXT,
  ADD COLUMN IF NOT EXISTS caption_status      TEXT,
  ADD COLUMN IF NOT EXISTS caption_error       TEXT;

-- Rollback:
--   ALTER TABLE public.videos
--     DROP COLUMN IF EXISTS captioned_video_url,
--     DROP COLUMN IF EXISTS caption_status,
--     DROP COLUMN IF EXISTS caption_error;
