CREATE TABLE bbs_publication_steps (
  video_analysis_id uuid NOT NULL
    REFERENCES video_analyses(id) ON DELETE RESTRICT,
  target_key text NOT NULL,
  step text NOT NULL,
  topic_title text,
  markdown_snapshot text,
  content_sha256 text GENERATED ALWAYS AS (
    encode(digest(markdown_snapshot, 'sha256'), 'hex')
  ) STORED,
  status text NOT NULL DEFAULT 'pending',
  remote_topic_id text,
  remote_comment_id bigint,
  remote_status integer,
  attempt_count integer NOT NULL DEFAULT 0,
  error_message text,
  request_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  response_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (video_analysis_id, target_key, step),
  CONSTRAINT bbs_publication_steps_target_key_not_blank CHECK (
    NULLIF(btrim(target_key), '') IS NOT NULL
  ),
  CONSTRAINT bbs_publication_steps_step_check CHECK (
    step IN ('topic', 'translation', 'source')
  ),
  CONSTRAINT bbs_publication_steps_status_check CHECK (
    status IN (
      'pending',
      'claimed',
      'in_progress',
      'created',
      'succeeded',
      'failed',
      'uncertain',
      'skipped'
    )
  ),
  CONSTRAINT bbs_publication_steps_attempt_non_negative CHECK (
    attempt_count >= 0
  ),
  CONSTRAINT bbs_publication_steps_topic_title_check CHECK (
    (
      step = 'topic'
      AND NULLIF(btrim(topic_title), '') IS NOT NULL
      AND char_length(topic_title) <= 128
    )
    OR (step IN ('translation', 'source') AND topic_title IS NULL)
  ),
  CONSTRAINT bbs_publication_steps_content_check CHECK (
    (status = 'skipped' AND markdown_snapshot IS NULL)
    OR (
      status <> 'skipped'
      AND NULLIF(btrim(markdown_snapshot), '') IS NOT NULL
    )
  ),
  CONSTRAINT bbs_publication_steps_remote_topic_id_not_blank CHECK (
    remote_topic_id IS NULL OR NULLIF(btrim(remote_topic_id), '') IS NOT NULL
  ),
  CONSTRAINT bbs_publication_steps_remote_comment_id_positive CHECK (
    remote_comment_id IS NULL OR remote_comment_id > 0
  ),
  CONSTRAINT bbs_publication_steps_remote_status_check CHECK (
    remote_status IS NULL OR remote_status >= 0
  ),
  CONSTRAINT bbs_publication_steps_remote_id_type_check CHECK (
    (step = 'topic' AND remote_comment_id IS NULL)
    OR (
      step IN ('translation', 'source')
      AND remote_topic_id IS NULL
    )
  ),
  CONSTRAINT bbs_publication_steps_request_metadata_check CHECK (
    jsonb_typeof(request_metadata) = 'object'
    AND (
      NOT (request_metadata ? 'portal_target')
      OR (
        jsonb_typeof(request_metadata -> 'portal_target') = 'object'
        AND (request_metadata -> 'portal_target') ?& ARRAY[
          'origin',
          'user_id',
          'category_id',
          'category_name',
          'username'
        ]
        AND (request_metadata -> 'portal_target') - ARRAY[
          'origin',
          'user_id',
          'category_id',
          'category_name',
          'username'
        ] = '{}'::jsonb
        AND jsonb_typeof(request_metadata #> '{portal_target,origin}') = 'string'
        AND NULLIF(btrim(request_metadata #>> '{portal_target,origin}'), '') IS NOT NULL
        AND request_metadata #>> '{portal_target,origin}'
          = btrim(request_metadata #>> '{portal_target,origin}')
        AND jsonb_typeof(request_metadata #> '{portal_target,user_id}') = 'string'
        AND NULLIF(btrim(request_metadata #>> '{portal_target,user_id}'), '') IS NOT NULL
        AND request_metadata #>> '{portal_target,user_id}'
          = btrim(request_metadata #>> '{portal_target,user_id}')
        AND jsonb_typeof(request_metadata #> '{portal_target,category_name}') = 'string'
        AND NULLIF(
          btrim(request_metadata #>> '{portal_target,category_name}'),
          ''
        ) IS NOT NULL
        AND request_metadata #>> '{portal_target,category_name}'
          = btrim(request_metadata #>> '{portal_target,category_name}')
        AND jsonb_typeof(request_metadata #> '{portal_target,username}') = 'string'
        AND NULLIF(
          btrim(request_metadata #>> '{portal_target,username}'),
          ''
        ) IS NOT NULL
        AND request_metadata #>> '{portal_target,username}'
          = btrim(request_metadata #>> '{portal_target,username}')
        AND CASE
          WHEN jsonb_typeof(
            request_metadata #> '{portal_target,category_id}'
          ) = 'number'
          THEN (request_metadata #>> '{portal_target,category_id}')::numeric > 0
            AND trunc(
              (request_metadata #>> '{portal_target,category_id}')::numeric
            ) = (request_metadata #>> '{portal_target,category_id}')::numeric
          ELSE false
        END
      )
    )
  ),
  CONSTRAINT bbs_publication_steps_attempt_target_check CHECK (
    status IN ('pending', 'skipped')
    OR request_metadata ? 'portal_target'
  ),
  CONSTRAINT bbs_publication_steps_state_fields_check CHECK (
    (
      status = 'pending'
      AND attempt_count = 0
      AND remote_topic_id IS NULL
      AND remote_comment_id IS NULL
      AND remote_status IS NULL
      AND error_message IS NULL
      AND started_at IS NULL
      AND completed_at IS NULL
    )
    OR (
      status = 'claimed'
      AND attempt_count > 0
      AND remote_topic_id IS NULL
      AND remote_comment_id IS NULL
      AND remote_status IS NULL
      AND error_message IS NULL
      AND started_at IS NULL
      AND completed_at IS NULL
    )
    OR (
      status = 'in_progress'
      AND attempt_count > 0
      AND remote_topic_id IS NULL
      AND remote_comment_id IS NULL
      AND remote_status IS NULL
      AND error_message IS NULL
      AND started_at IS NOT NULL
      AND completed_at IS NULL
    )
    OR (
      status = 'created'
      AND attempt_count > 0
      AND (
        (step = 'topic' AND remote_topic_id IS NOT NULL)
        OR (
          step IN ('translation', 'source')
          AND remote_comment_id IS NOT NULL
        )
      )
      AND error_message IS NULL
      AND started_at IS NOT NULL
      AND completed_at IS NULL
    )
    OR (
      status = 'succeeded'
      AND attempt_count > 0
      AND (
        (step = 'topic' AND remote_topic_id IS NOT NULL)
        OR (
          step IN ('translation', 'source')
          AND remote_comment_id IS NOT NULL
        )
      )
      AND error_message IS NULL
      AND started_at IS NOT NULL
      AND completed_at IS NOT NULL
    )
    OR (
      status IN ('failed', 'uncertain')
      AND attempt_count > 0
      AND remote_topic_id IS NULL
      AND remote_comment_id IS NULL
      AND remote_status IS NULL
      AND NULLIF(btrim(error_message), '') IS NOT NULL
      AND started_at IS NOT NULL
      AND completed_at IS NOT NULL
    )
    OR (
      status = 'skipped'
      AND step = 'source'
      AND attempt_count = 0
      AND remote_topic_id IS NULL
      AND remote_comment_id IS NULL
      AND remote_status IS NULL
      AND error_message IS NULL
      AND started_at IS NULL
      AND completed_at IS NOT NULL
    )
  ),
  CONSTRAINT bbs_publication_steps_time_order_check CHECK (
    completed_at IS NULL
    OR started_at IS NULL
    OR completed_at >= started_at
  )
);

CREATE INDEX idx_bbs_publication_steps_automatic
  ON bbs_publication_steps(
    target_key,
    status,
    updated_at,
    video_analysis_id,
    step
  )
  WHERE status IN ('pending', 'claimed', 'in_progress', 'created', 'failed');

CREATE FUNCTION reject_bbs_publication_immutable_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF ROW(
    NEW.video_analysis_id,
    NEW.target_key,
    NEW.step,
    NEW.topic_title,
    NEW.markdown_snapshot
  ) IS DISTINCT FROM ROW(
    OLD.video_analysis_id,
    OLD.target_key,
    OLD.step,
    OLD.topic_title,
    OLD.markdown_snapshot
  ) THEN
    RAISE EXCEPTION 'bbs publication target and content snapshot are immutable';
  END IF;

  IF OLD.request_metadata ? 'portal_target'
     AND NEW.request_metadata -> 'portal_target'
         IS DISTINCT FROM OLD.request_metadata -> 'portal_target' THEN
    RAISE EXCEPTION 'bbs publication portal target is immutable after binding';
  END IF;

  RETURN NEW;
END;
$$;

CREATE TRIGGER bbs_publication_steps_immutable
BEFORE UPDATE OF
  video_analysis_id,
  target_key,
  step,
  topic_title,
  markdown_snapshot,
  request_metadata
ON bbs_publication_steps
FOR EACH ROW
EXECUTE FUNCTION reject_bbs_publication_immutable_update();
