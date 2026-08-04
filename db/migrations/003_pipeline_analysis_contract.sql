ALTER TABLE subtitle_tracks
  ADD COLUMN translated_text text,
  ADD COLUMN translated_language_code text,
  ADD COLUMN translation_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD CONSTRAINT subtitle_tracks_translation_pair_check CHECK (
    (translated_text IS NULL) = (translated_language_code IS NULL)
  );

ALTER TABLE analysis_runs
  ADD COLUMN video_id uuid REFERENCES videos(id) ON DELETE CASCADE,
  ADD COLUMN subtitle_track_id uuid REFERENCES subtitle_tracks(id) ON DELETE SET NULL,
  ADD CONSTRAINT analysis_runs_subtitle_video_fk
    FOREIGN KEY (subtitle_track_id, video_id)
    REFERENCES subtitle_tracks(id, video_id);

ALTER TABLE video_analyses
  ADD COLUMN profile_name text NOT NULL DEFAULT 'default',
  ADD COLUMN output_schema_version text,
  ADD COLUMN analysis_metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX idx_analysis_runs_video_id
  ON analysis_runs(video_id);

CREATE INDEX idx_video_analyses_profile_schema
  ON video_analyses(video_id, profile_name, output_schema_version);

CREATE INDEX idx_video_analyses_raw_agent_output
  ON video_analyses USING gin(raw_agent_output jsonb_path_ops);
