ALTER TABLE subtitle_tracks
  ADD COLUMN raw_sha256 text GENERATED ALWAYS AS (
    encode(digest(raw_text, 'sha256'), 'hex')
  ) STORED;

ALTER TABLE subtitle_tracks
  DROP CONSTRAINT subtitle_tracks_video_language_unique,
  ADD CONSTRAINT subtitle_tracks_video_language_source_unique
    UNIQUE (video_id, language_code, is_auto_generated, raw_sha256);
