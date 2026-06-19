-- Cinematic video mode columns (supersedes db/add_video_mode.sql — includes the mode column too,
-- so running THIS one file is enough). Both statements are idempotent and safe on a live DB.
--
--  mode               : 'avatar' (scripted promo) | 'cinematic' (Seedance clip). Poller branches on it.
--  heygen_segment_ids : JSON array of HeyGen video_ids for a multi-clip cinematic (30s/45s = 2/3
--                       segments, in play order). NULL for single-clip / avatar videos. When set
--                       (len > 1) the poller waits for all segments, ffmpeg-stitches them, then
--                       captions + appends the Allegiance outro.
ALTER TABLE public.videos
    ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'avatar';
ALTER TABLE public.videos
    ADD COLUMN IF NOT EXISTS heygen_segment_ids jsonb;

-- Rollback:
-- ALTER TABLE public.videos DROP COLUMN IF EXISTS heygen_segment_ids;
-- ALTER TABLE public.videos DROP COLUMN IF EXISTS mode;
