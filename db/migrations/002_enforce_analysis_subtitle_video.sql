ALTER TABLE subtitle_tracks
  ADD CONSTRAINT subtitle_tracks_id_video_id_unique UNIQUE (id, video_id);

ALTER TABLE video_analyses
  ADD CONSTRAINT video_analyses_subtitle_video_fk
  FOREIGN KEY (subtitle_track_id, video_id)
  REFERENCES subtitle_tracks (id, video_id);

CREATE INDEX idx_video_analyses_subtitle_video
  ON video_analyses(subtitle_track_id, video_id);
