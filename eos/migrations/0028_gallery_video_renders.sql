-- Auto-rendered gallery slideshow / reel videos (Wave 2, item 6).
--
-- One row per (gallery, format): 'slideshow' is 16:9 1080p landscape,
-- 'reel' is 9:16 1080x1920 vertical for social. Rendering runs through the
-- SQLite job queue (kind 'gallery_video_render'); this table is the durable
-- status surface for the admin gallery page and the delivery surfaces.

CREATE TABLE IF NOT EXISTS gallery_video_renders (
    id              INTEGER PRIMARY KEY,
    studio_id       TEXT NOT NULL REFERENCES studio(id) ON DELETE CASCADE,
    gallery_id      INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
    format          TEXT NOT NULL CHECK (format IN ('slideshow','reel')),
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','rendering','ready','failed')),
    job_id          INTEGER,
    file_path       TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (gallery_id, format)
);
CREATE INDEX IF NOT EXISTS idx_gallery_video_renders_studio
    ON gallery_video_renders (studio_id, gallery_id, status);
