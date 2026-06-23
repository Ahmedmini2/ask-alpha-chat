-- Personal-branding image generations (Nano Banana Pro). One row per generated image so each
-- agent keeps a gallery/history. Web-app-owned style: RLS on; the backend reads/writes via the
-- BYPASSRLS `postgres` role, while the web app reads each agent's own rows through Supabase REST.
CREATE TABLE IF NOT EXISTS public.branding_images (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    template_slug text NOT NULL,
    overlay_text  text,
    s3_key        text,          -- durable S3 key (re-presign for long-lived links)
    image_url     text,          -- presigned URL at creation time (expires in 7 days)
    status        text NOT NULL DEFAULT 'completed',
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS branding_images_user_created_idx
    ON public.branding_images (user_id, created_at DESC);

ALTER TABLE public.branding_images ENABLE ROW LEVEL SECURITY;
-- The web app (anon/auth Supabase REST) may read an agent's OWN gallery. The backend bypasses RLS.
DROP POLICY IF EXISTS branding_images_owner_read ON public.branding_images;
CREATE POLICY branding_images_owner_read ON public.branding_images
    FOR SELECT USING (auth.uid() = user_id);

-- Rollback:
-- DROP TABLE IF EXISTS public.branding_images;
