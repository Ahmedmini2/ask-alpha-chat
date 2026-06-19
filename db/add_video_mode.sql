-- Generation mode for promo videos.
-- 'avatar'    = the scripted HeyGen avatar promo (house-style narration + AI background + b-roll +
--               captions + optional outro) — the existing behaviour.
-- 'cinematic' = HeyGen Cinematic Avatar (Seedance): a ~15s clip generated from a natural-language
--               prompt + the agent's own avatar look(s) + the project's photos as references. The
--               poller branches on this column (cinematic skips b-roll). Idempotent.
ALTER TABLE public.videos
    ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'avatar';

-- Rollback:
-- ALTER TABLE public.videos DROP COLUMN IF EXISTS mode;
