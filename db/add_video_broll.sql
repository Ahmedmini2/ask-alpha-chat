-- B-roll post-edit columns for the videos table (Supabase project pqzsdxcjyqjjvfsunzak).
--
-- After HeyGen renders the avatar video, an ffmpeg step cuts full-screen Ken-Burns stills of
-- the property into the middle (avatar holds the hook + CTA) while narration plays underneath,
-- then Descript captions the composite. These columns track that step.
--
--   broll_video_url : presigned S3 URL of the composited (but UNCAPTIONED) mp4 in
--                     generated/videos/* — delivered only when captioning later fails.
--   broll_status    : NULL | 'processing' | 'done' | 'skipped' | 'failed'
--   broll_error     : failure detail when broll_status = 'failed'
--
-- Additive + nullable, safe on the shared production DB and trivially reversible. The
-- videos.status CHECK is untouched (b-roll uses its own column, so the row stays in
-- pending/processing/completed/failed throughout). Run in the Supabase SQL editor, via
-- `supabase db push`, or the Supabase MCP apply_migration.

ALTER TABLE public.videos
  ADD COLUMN IF NOT EXISTS broll_video_url TEXT,
  ADD COLUMN IF NOT EXISTS broll_status    TEXT,
  ADD COLUMN IF NOT EXISTS broll_error     TEXT;

-- Rollback:
--   ALTER TABLE public.videos
--     DROP COLUMN IF EXISTS broll_video_url,
--     DROP COLUMN IF EXISTS broll_status,
--     DROP COLUMN IF EXISTS broll_error;
