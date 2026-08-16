ALTER TABLE video_analyses
  ADD COLUMN translated_subtitle_snapshot text;

UPDATE video_analyses AS analysis
SET translated_subtitle_snapshot = subtitle.translated_text
FROM subtitle_tracks AS subtitle
JOIN analysis_runs AS run
  ON run.subtitle_track_id = subtitle.id
 AND run.video_id = subtitle.video_id
WHERE subtitle.id = analysis.subtitle_track_id
  AND subtitle.video_id = analysis.video_id
  AND run.id = analysis.analysis_run_id
  AND run.status = 'succeeded'
  AND run.metadata -> 'translation' = subtitle.translation_metadata
  AND NULLIF(btrim(subtitle.translated_text), '') IS NOT NULL;

CREATE FUNCTION enforce_video_analysis_translation_snapshot()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'INSERT'
     AND NULLIF(btrim(NEW.translated_subtitle_snapshot), '') IS NULL THEN
    RAISE EXCEPTION 'new video analysis requires a translated subtitle snapshot'
      USING ERRCODE = '23514';
  END IF;

  IF TG_OP = 'UPDATE'
     AND NEW.translated_subtitle_snapshot
         IS DISTINCT FROM OLD.translated_subtitle_snapshot THEN
    RAISE EXCEPTION 'video analysis translated subtitle snapshot is immutable';
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER video_analyses_translation_snapshot_guard
BEFORE INSERT OR UPDATE ON video_analyses
FOR EACH ROW
EXECUTE FUNCTION enforce_video_analysis_translation_snapshot();

CREATE INDEX idx_video_analyses_publication_fifo
  ON video_analyses(analyzed_at, created_at, id)
  WHERE NULLIF(btrim(translated_subtitle_snapshot), '') IS NOT NULL;
