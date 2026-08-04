ALTER TABLE analysis_runs
  ADD CONSTRAINT analysis_runs_identity_unique
    UNIQUE (id, video_id, subtitle_track_id);

ALTER TABLE video_analyses
  ADD CONSTRAINT video_analyses_run_identity_fk
    FOREIGN KEY (analysis_run_id, video_id, subtitle_track_id)
    REFERENCES analysis_runs(id, video_id, subtitle_track_id)
    NOT VALID;

CREATE INDEX idx_video_analyses_run_identity
  ON video_analyses(analysis_run_id, video_id, subtitle_track_id);
