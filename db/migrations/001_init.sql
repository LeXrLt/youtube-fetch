CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS researchers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name text NOT NULL,
  affiliation text,
  homepage_url text,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT researchers_display_name_unique UNIQUE (display_name)
);

CREATE TABLE IF NOT EXISTS youtube_channels (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  researcher_id uuid REFERENCES researchers(id) ON DELETE SET NULL,
  youtube_channel_id text NOT NULL,
  handle text,
  title text NOT NULL,
  channel_url text NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  last_checked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT youtube_channels_channel_id_unique UNIQUE (youtube_channel_id),
  CONSTRAINT youtube_channels_channel_url_unique UNIQUE (channel_url)
);

CREATE TABLE IF NOT EXISTS videos (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  channel_id uuid NOT NULL REFERENCES youtube_channels(id) ON DELETE CASCADE,
  youtube_video_id text NOT NULL,
  title text NOT NULL,
  description text,
  video_url text NOT NULL,
  duration_seconds integer,
  published_at timestamptz,
  downloaded_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT videos_youtube_video_id_unique UNIQUE (youtube_video_id),
  CONSTRAINT videos_duration_non_negative CHECK (duration_seconds IS NULL OR duration_seconds >= 0)
);

CREATE TABLE IF NOT EXISTS subtitle_tracks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  video_id uuid NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  language_code text NOT NULL,
  language_name text,
  source_format text,
  is_auto_generated boolean NOT NULL DEFAULT false,
  raw_text text NOT NULL,
  normalized_text text,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT subtitle_tracks_video_language_unique UNIQUE (video_id, language_code, is_auto_generated)
);

CREATE TABLE IF NOT EXISTS analysis_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_name text NOT NULL DEFAULT 'codex',
  agent_model text,
  prompt_version text,
  status text NOT NULL DEFAULT 'pending',
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  error_message text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT analysis_runs_status_check CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS video_analyses (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  video_id uuid NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  subtitle_track_id uuid REFERENCES subtitle_tracks(id) ON DELETE SET NULL,
  analysis_run_id uuid REFERENCES analysis_runs(id) ON DELETE SET NULL,
  relevance_score numeric(5, 2),
  quality_score numeric(5, 2),
  is_relevant boolean NOT NULL DEFAULT true,
  summary text,
  translated_summary text,
  background_notes text,
  key_points jsonb NOT NULL DEFAULT '[]'::jsonb,
  raw_agent_output jsonb NOT NULL DEFAULT '{}'::jsonb,
  analyzed_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT video_analyses_relevance_score_check CHECK (relevance_score IS NULL OR (relevance_score >= 0 AND relevance_score <= 100)),
  CONSTRAINT video_analyses_quality_score_check CHECK (quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 100))
);

CREATE TABLE IF NOT EXISTS tags (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  category text,
  description text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT tags_name_unique UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS video_analysis_tags (
  video_analysis_id uuid NOT NULL REFERENCES video_analyses(id) ON DELETE CASCADE,
  tag_id uuid NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  confidence numeric(5, 2),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (video_analysis_id, tag_id),
  CONSTRAINT video_analysis_tags_confidence_check CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 100))
);

CREATE INDEX IF NOT EXISTS idx_youtube_channels_researcher_id ON youtube_channels(researcher_id);
CREATE INDEX IF NOT EXISTS idx_videos_channel_id ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_published_at ON videos(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_subtitle_tracks_video_id ON subtitle_tracks(video_id);
CREATE INDEX IF NOT EXISTS idx_video_analyses_video_id ON video_analyses(video_id);
CREATE INDEX IF NOT EXISTS idx_video_analyses_relevance_score ON video_analyses(relevance_score DESC);
CREATE INDEX IF NOT EXISTS idx_tags_category ON tags(category);
