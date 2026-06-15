-- Allegiance outro opt-in for promo videos.
-- When true, the poller appends the orientation-correct Allegiance outro (with a short crossfade)
-- as the final post-edit step, after b-roll and captions. Set per-video at creation time from the
-- agent's answer to "Do you want to add the Allegiance outro?". Idempotent.
ALTER TABLE public.videos
    ADD COLUMN IF NOT EXISTS add_outro boolean NOT NULL DEFAULT false;

-- Rollback:
-- ALTER TABLE public.videos DROP COLUMN IF EXISTS add_outro;
