DROP INDEX IF EXISTS idx_videos_published_at;
CREATE INDEX idx_videos_published_at
  ON videos(published_at DESC NULLS LAST);

DROP INDEX IF EXISTS idx_videos_subtitle_download_queue;
CREATE INDEX idx_videos_subtitle_download_queue
  ON videos(
    published_at DESC NULLS LAST,
    subtitle_download_status,
    subtitle_checked_at ASC NULLS FIRST,
    created_at,
    id
  )
  WHERE subtitle_download_status IN (0, 2);
