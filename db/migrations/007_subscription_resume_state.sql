ALTER TABLE youtube_channels
  ADD COLUMN is_subscribed boolean NOT NULL DEFAULT false,
  ADD COLUMN subscription_last_seen_at timestamptz,
  ADD COLUMN initial_backfill_completed_at timestamptz;

ALTER TABLE videos
  ADD COLUMN subtitle_status text NOT NULL DEFAULT 'pending',
  ADD COLUMN subtitle_checked_at timestamptz,
  ADD CONSTRAINT videos_subtitle_status_check CHECK (
    subtitle_status IN ('pending', 'fetched', 'unavailable', 'invalid')
  );

UPDATE videos
SET subtitle_status = CASE
      WHEN EXISTS (
        SELECT 1
        FROM subtitle_tracks
        WHERE subtitle_tracks.video_id = videos.id
          AND subtitle_tracks.normalized_text IS NOT NULL
      ) THEN 'fetched'
      WHEN EXISTS (
        SELECT 1
        FROM subtitle_tracks
        WHERE subtitle_tracks.video_id = videos.id
      ) THEN 'invalid'
      ELSE 'pending'
    END,
    subtitle_checked_at = downloaded_at
WHERE subtitle_status = 'pending';

CREATE INDEX idx_youtube_channels_subscribed
  ON youtube_channels(is_subscribed, title)
  WHERE is_subscribed = true;

CREATE INDEX idx_youtube_channels_backfill
  ON youtube_channels(initial_backfill_completed_at)
  WHERE is_active = true;

CREATE INDEX idx_videos_subtitle_status
  ON videos(channel_id, subtitle_status, published_at DESC);
