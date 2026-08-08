ALTER TABLE videos
  ADD COLUMN subtitle_download_status smallint NOT NULL DEFAULT 0,
  ADD COLUMN subtitle_download_error text,
  ADD CONSTRAINT videos_subtitle_download_status_check CHECK (
    subtitle_download_status IN (0, 1, 2)
  ),
  ADD CONSTRAINT videos_subtitle_download_error_check CHECK (
    (subtitle_download_status = 2) = (subtitle_download_error IS NOT NULL)
  );

UPDATE videos
SET subtitle_download_status = CASE
      WHEN subtitle_status = 'pending' THEN 0
      ELSE 1
    END;

CREATE INDEX idx_videos_subtitle_download_queue
  ON videos(
    subtitle_download_status,
    subtitle_checked_at ASC NULLS FIRST,
    created_at,
    id
  )
  WHERE subtitle_download_status IN (0, 2);
