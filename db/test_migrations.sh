#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
POSTGRES_VARIABLES=(
  POSTGRES_HOST
  POSTGRES_PORT
  POSTGRES_USER
  POSTGRES_PASSWORD
)

declare -A ENV_OVERRIDES=()
for variable in "${POSTGRES_VARIABLES[@]}"; do
  if [[ -v $variable ]]; then
    ENV_OVERRIDES["$variable"]="${!variable}"
  fi
done

if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

for variable in "${!ENV_OVERRIDES[@]}"; do
  printf -v "$variable" '%s' "${ENV_OVERRIDES[$variable]}"
  export "$variable"
done

: "${POSTGRES_HOST:=localhost}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_USER:?POSTGRES_USER is required in the environment or $ENV_FILE}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required in the environment or $ENV_FILE}"

for command_name in psql createdb dropdb od tr; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "$command_name is required to test database migrations" >&2
    exit 1
  fi
done

random_hex() {
  od -An -N16 -tx1 /dev/urandom | tr -d ' \n'
}

TEST_DATABASE="youtube_fetch_test_$(random_hex)"
OWNERSHIP_TOKEN="$(random_hex)"
TEMP_ROOT=""
DATABASE_RESERVED=false
OWNERSHIP_MARKED=false
first_pid=""
second_pid=""
export POSTGRES_HOST POSTGRES_PORT POSTGRES_USER POSTGRES_PASSWORD
export POSTGRES_DB="$TEST_DATABASE"
export PGPASSWORD="$POSTGRES_PASSWORD"

cleanup() {
  local status=$?
  local cleanup_failed=0
  local ownership_matches
  trap - EXIT INT TERM
  set +e

  for child_pid in "$first_pid" "$second_pid"; do
    if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
      kill "$child_pid" 2>/dev/null
    fi
  done
  for child_pid in "$first_pid" "$second_pid"; do
    if [ -n "$child_pid" ]; then
      wait "$child_pid" 2>/dev/null
    fi
  done

  if [ -n "$TEMP_ROOT" ]; then
    if ! command rm -rf -- "$TEMP_ROOT"; then
      echo "Failed to remove migration test files at $TEMP_ROOT" >&2
      cleanup_failed=1
    fi
  fi

  if [ "$DATABASE_RESERVED" = true ]; then
    if [ "$OWNERSHIP_MARKED" = true ]; then
      ownership_matches="$(
        printf '%s\n' "SELECT EXISTS (SELECT 1 FROM public.migration_test_ownership WHERE token = :'ownership_token')::integer;" |
          psql \
            --no-psqlrc \
            --host "$POSTGRES_HOST" \
            --port "$POSTGRES_PORT" \
            --username "$POSTGRES_USER" \
            --no-password \
            --dbname "$TEST_DATABASE" \
            --tuples-only \
            --no-align \
            --set ON_ERROR_STOP=1 \
            --set "ownership_token=$OWNERSHIP_TOKEN"
      )"
    else
      # A successful createdb call proves ownership before the marker is written.
      ownership_matches=1
    fi

    if [ "$ownership_matches" != "1" ]; then
      echo "Refusing to drop unverified test database $TEST_DATABASE" >&2
      cleanup_failed=1
    elif ! dropdb \
      --host "$POSTGRES_HOST" \
      --port "$POSTGRES_PORT" \
      --username "$POSTGRES_USER" \
      --no-password \
      "$TEST_DATABASE"; then
      echo "Failed to drop migration test database $TEST_DATABASE" >&2
      cleanup_failed=1
    fi
  fi

  if [ "$status" -eq 0 ] && [ "$cleanup_failed" -ne 0 ]; then
    status=1
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

if ! createdb \
  --host "$POSTGRES_HOST" \
  --port "$POSTGRES_PORT" \
  --username "$POSTGRES_USER" \
  --no-password \
  "$TEST_DATABASE"; then
  echo "Failed to reserve migration test database $TEST_DATABASE" >&2
  exit 1
fi
DATABASE_RESERVED=true

if ! printf '%s\n' \
  "CREATE TABLE public.migration_test_ownership (token text PRIMARY KEY);" \
  "INSERT INTO public.migration_test_ownership(token) VALUES (:'ownership_token');" |
  psql \
    --no-psqlrc \
    --host "$POSTGRES_HOST" \
    --port "$POSTGRES_PORT" \
    --username "$POSTGRES_USER" \
    --no-password \
    --dbname "$TEST_DATABASE" \
    --set ON_ERROR_STOP=1 \
    --set "ownership_token=$OWNERSHIP_TOKEN"; then
  echo "Failed to mark ownership of migration test database $TEST_DATABASE" >&2
  exit 1
fi
OWNERSHIP_MARKED=true

run_migrations() {
  ENV_FILE=/dev/null "$ROOT_DIR/db/migrate.sh"
}

# Exercise first-time schema migration and the advisory lock with two deploy hooks.
run_migrations &
first_pid=$!
run_migrations &
second_pid=$!
first_status=0
second_status=0
wait "$first_pid" || first_status=$?
wait "$second_pid" || second_status=$?
first_pid=""
second_pid=""
if [ "$first_status" -ne 0 ] || [ "$second_status" -ne 0 ]; then
  echo "Concurrent migration process failed" >&2
  exit 1
fi

# The exported test database must override the conflicting value in the root .env.
ENV_FILE="$ROOT_DIR/.env" "$ROOT_DIR/db/migrate.sh"

PSQL_TEST=(
  psql
  --no-psqlrc
  --host "$POSTGRES_HOST"
  --port "$POSTGRES_PORT"
  --username "$POSTGRES_USER"
  --no-password
  --dbname "$TEST_DATABASE"
  --set ON_ERROR_STOP=1
)

assert_sql_rejected() {
  local failure_message="$1"

  if "${PSQL_TEST[@]}" >/dev/null 2>&1; then
    echo "$failure_message" >&2
    exit 1
  fi
}

table_count="$(
  "${PSQL_TEST[@]}" --tuples-only --no-align --command \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('schema_migrations', 'researchers', 'youtube_channels', 'videos', 'subtitle_tracks', 'analysis_runs', 'agent_invocations', 'video_analyses', 'tags', 'video_analysis_tags', 'bbs_publication_steps');"
)"
expected_migration_count=0
for migration in "$ROOT_DIR/db/migrations"/*.sql; do
  [ -e "$migration" ] || continue
  expected_migration_count=$((expected_migration_count + 1))
done
migration_count="$(
  "${PSQL_TEST[@]}" --tuples-only --no-align --command \
    "SELECT count(*) FROM schema_migrations WHERE length(checksum) = 64;"
)"

[ "$table_count" = "11" ]
[ "$migration_count" = "$expected_migration_count" ]

"${PSQL_TEST[@]}" >/dev/null <<'SQL'
INSERT INTO researchers(id, display_name)
VALUES ('00000000-0000-0000-0000-000000000001', 'Migration test');

INSERT INTO youtube_channels(id, researcher_id, youtube_channel_id, title, channel_url)
VALUES (
  '00000000-0000-0000-0000-000000000002',
  '00000000-0000-0000-0000-000000000001',
  'migration-test-channel',
  'Migration test',
  'https://example.invalid/channel'
);

INSERT INTO videos(id, channel_id, youtube_video_id, title, video_url)
VALUES
  (
    '00000000-0000-0000-0000-000000000003',
    '00000000-0000-0000-0000-000000000002',
    'migration-test-video-a',
    'Video A',
    'https://example.invalid/video-a'
  ),
  (
    '00000000-0000-0000-0000-000000000004',
    '00000000-0000-0000-0000-000000000002',
    'migration-test-video-b',
    'Video B',
    'https://example.invalid/video-b'
  );

INSERT INTO subtitle_tracks(id, video_id, language_code, raw_text)
VALUES (
  '00000000-0000-0000-0000-000000000005',
  '00000000-0000-0000-0000-000000000004',
  'en',
  'Migration test subtitle'
);

INSERT INTO subtitle_tracks(
  id,
  video_id,
  language_code,
  raw_text,
  normalized_text,
  translated_text,
  translated_language_code,
  translation_metadata
)
VALUES
  (
    '00000000-0000-0000-0000-000000000006',
    '00000000-0000-0000-0000-000000000004',
    'zh-Hant',
    'Backfill raw source',
    'Backfill normalized source',
    NULL,
    NULL,
    '{"legacy": true, "mode": "legacy"}'::jsonb
  ),
  (
    '00000000-0000-0000-0000-000000000007',
    '00000000-0000-0000-0000-000000000004',
    'ZH-CN',
    'Uppercase language raw source',
    'Uppercase language normalized source',
    NULL,
    NULL,
    '{}'::jsonb
  ),
  (
    '00000000-0000-0000-0000-000000000008',
    '00000000-0000-0000-0000-000000000004',
    'en',
    'English raw source',
    'English normalized source',
    NULL,
    NULL,
    '{}'::jsonb
  ),
  (
    '00000000-0000-0000-0000-000000000009',
    '00000000-0000-0000-0000-000000000004',
    'zh',
    'Missing normalized source',
    NULL,
    NULL,
    NULL,
    '{}'::jsonb
  ),
  (
    '00000000-0000-0000-0000-000000000010',
    '00000000-0000-0000-0000-000000000004',
    'zh-Hans',
    'Existing translation raw source',
    'Existing translation normalized source',
    'Existing translated text',
    'zh-Hans',
    '{"mode": "existing", "keep": true}'::jsonb
  ),
  (
    '00000000-0000-0000-0000-000000000011',
    '00000000-0000-0000-0000-000000000004',
    'zh-TW',
    'Blank normalized source',
    '   ',
    NULL,
    NULL,
    '{}'::jsonb
  );
SQL

"${PSQL_TEST[@]}" \
  --file "$ROOT_DIR/db/migrations/012_backfill_chinese_translations.sql" \
  >/dev/null

translation_backfill_valid="$(
  "${PSQL_TEST[@]}" --tuples-only --no-align --command \
    "SELECT (
      (SELECT translated_text = 'Backfill normalized source'
              AND translated_language_code = 'zh-Hant'
              AND translation_metadata ->> 'mode' = 'copied_chinese_source'
              AND translation_metadata ->> 'migration' = '012_backfill_chinese_translations'
              AND translation_metadata ->> 'legacy' = 'true'
       FROM subtitle_tracks
       WHERE id = '00000000-0000-0000-0000-000000000006')
      AND
      (SELECT translated_text = 'Uppercase language normalized source'
              AND translated_language_code = 'ZH-CN'
       FROM subtitle_tracks
       WHERE id = '00000000-0000-0000-0000-000000000007')
      AND
      (SELECT translated_text IS NULL AND translated_language_code IS NULL
       FROM subtitle_tracks
       WHERE id = '00000000-0000-0000-0000-000000000008')
      AND
      (SELECT translated_text IS NULL AND translated_language_code IS NULL
       FROM subtitle_tracks
       WHERE id = '00000000-0000-0000-0000-000000000009')
      AND
      (SELECT translated_text = 'Existing translated text'
              AND translated_language_code = 'zh-Hans'
              AND translation_metadata = '{\"mode\": \"existing\", \"keep\": true}'::jsonb
       FROM subtitle_tracks
       WHERE id = '00000000-0000-0000-0000-000000000010')
      AND
      (SELECT translated_text IS NULL AND translated_language_code IS NULL
       FROM subtitle_tracks
       WHERE id = '00000000-0000-0000-0000-000000000011')
    )::integer;"
)"
[ "$translation_backfill_valid" = "1" ]

if "${PSQL_TEST[@]}" >/dev/null 2>&1 <<'SQL'
INSERT INTO video_analyses(video_id, subtitle_track_id)
VALUES (
  '00000000-0000-0000-0000-000000000003',
  '00000000-0000-0000-0000-000000000005'
);
SQL
then
  echo "Cross-video subtitle analysis was not rejected" >&2
  exit 1
fi

"${PSQL_TEST[@]}" >/dev/null <<'SQL'
INSERT INTO video_analyses(id, video_id, subtitle_track_id)
VALUES (
  '00000000-0000-0000-0000-000000000012',
  '00000000-0000-0000-0000-000000000004',
  '00000000-0000-0000-0000-000000000005'
);

DELETE FROM subtitle_tracks
WHERE id = '00000000-0000-0000-0000-000000000005';
SQL

cleared_reference="$(
  "${PSQL_TEST[@]}" --tuples-only --no-align --command \
    "SELECT (subtitle_track_id IS NULL)::integer FROM video_analyses WHERE video_id = '00000000-0000-0000-0000-000000000004';"
)"
[ "$cleared_reference" = "1" ]

"${PSQL_TEST[@]}" >/dev/null <<'SQL'
INSERT INTO bbs_publication_steps(
  video_analysis_id,
  target_key,
  step,
  topic_title,
  markdown_snapshot,
  status,
  completed_at
)
VALUES
  (
    '00000000-0000-0000-0000-000000000012',
    'portal-push:Youtube',
    'topic',
    'Migration publication topic',
    '# Migration publication topic',
    'pending',
    NULL
  ),
  (
    '00000000-0000-0000-0000-000000000012',
    'portal-push:Youtube',
    'translation',
    NULL,
    'Translated summary',
    'pending',
    NULL
  ),
  (
    '00000000-0000-0000-0000-000000000012',
    'portal-push:Youtube',
    'source',
    NULL,
    NULL,
    'skipped',
    '2026-01-01 00:00:00+00'
  );

UPDATE bbs_publication_steps
SET status = 'claimed',
    attempt_count = 1,
    request_metadata = '{
      "portal_target": {
        "origin": "https://portal.test",
        "user_id": "migration-user-id",
        "category_id": 4,
        "category_name": "Youtube",
        "username": "migration-publisher"
      }
    }'::jsonb,
    updated_at = '2026-01-01 00:01:00+00'
WHERE video_analysis_id = '00000000-0000-0000-0000-000000000012'
  AND target_key = 'portal-push:Youtube'
  AND step = 'topic';

UPDATE bbs_publication_steps
SET status = 'in_progress',
    started_at = '2026-01-01 00:01:30+00',
    updated_at = '2026-01-01 00:01:30+00'
WHERE video_analysis_id = '00000000-0000-0000-0000-000000000012'
  AND target_key = 'portal-push:Youtube'
  AND step = 'topic'
  AND status = 'claimed';

UPDATE bbs_publication_steps
SET status = 'created',
    remote_topic_id = 'migration-topic-123',
    remote_status = 2,
    response_metadata = '{"phase": "create"}'::jsonb,
    updated_at = '2026-01-01 00:02:00+00'
WHERE video_analysis_id = '00000000-0000-0000-0000-000000000012'
  AND target_key = 'portal-push:Youtube'
  AND step = 'topic';

UPDATE bbs_publication_steps
SET status = 'succeeded',
    remote_status = 0,
    completed_at = '2026-01-01 00:03:00+00',
    response_metadata = '{"phase": "readback"}'::jsonb,
    updated_at = '2026-01-01 00:03:00+00'
WHERE video_analysis_id = '00000000-0000-0000-0000-000000000012'
  AND target_key = 'portal-push:Youtube'
  AND step = 'topic';

UPDATE bbs_publication_steps
SET status = 'claimed',
    attempt_count = 1,
    request_metadata = '{
      "portal_target": {
        "origin": "https://portal.test",
        "user_id": "migration-user-id",
        "category_id": 4,
        "category_name": "Youtube",
        "username": "migration-publisher"
      }
    }'::jsonb,
    updated_at = '2026-01-01 00:04:00+00'
WHERE video_analysis_id = '00000000-0000-0000-0000-000000000012'
  AND target_key = 'portal-push:Youtube'
  AND step = 'translation';

UPDATE bbs_publication_steps
SET status = 'in_progress',
    started_at = '2026-01-01 00:04:30+00',
    updated_at = '2026-01-01 00:04:30+00'
WHERE video_analysis_id = '00000000-0000-0000-0000-000000000012'
  AND target_key = 'portal-push:Youtube'
  AND step = 'translation'
  AND status = 'claimed';

UPDATE bbs_publication_steps
SET status = 'failed',
    error_message = 'Migration test failure',
    response_metadata = '{"http_status": 503}'::jsonb,
    completed_at = '2026-01-01 00:05:00+00',
    updated_at = '2026-01-01 00:05:00+00'
WHERE video_analysis_id = '00000000-0000-0000-0000-000000000012'
  AND target_key = 'portal-push:Youtube'
  AND step = 'translation';

UPDATE bbs_publication_steps
SET status = 'claimed',
    attempt_count = attempt_count + 1,
    remote_status = NULL,
    error_message = NULL,
    started_at = NULL,
    completed_at = NULL,
    updated_at = '2026-01-01 00:06:00+00'
WHERE video_analysis_id = '00000000-0000-0000-0000-000000000012'
  AND target_key = 'portal-push:Youtube'
  AND step = 'translation'
  AND status = 'failed';

UPDATE bbs_publication_steps
SET status = 'in_progress',
    started_at = '2026-01-01 00:06:30+00',
    updated_at = '2026-01-01 00:06:30+00'
WHERE video_analysis_id = '00000000-0000-0000-0000-000000000012'
  AND target_key = 'portal-push:Youtube'
  AND step = 'translation'
  AND status = 'claimed';

UPDATE bbs_publication_steps
SET status = 'uncertain',
    error_message = 'Migration test uncertain outcome',
    completed_at = '2026-01-01 00:07:00+00',
    updated_at = '2026-01-01 00:07:00+00'
WHERE video_analysis_id = '00000000-0000-0000-0000-000000000012'
  AND target_key = 'portal-push:Youtube'
  AND step = 'translation'
  AND status = 'in_progress';
SQL

publication_steps_valid="$(
  "${PSQL_TEST[@]}" --tuples-only --no-align --command \
    "SELECT (
      count(*) = 3
      AND bool_and(
        content_sha256 IS NOT DISTINCT FROM
          encode(digest(markdown_snapshot, 'sha256'), 'hex')
      )
      AND bool_and(
        CASE step
          WHEN 'topic' THEN
            status = 'succeeded'
            AND remote_topic_id = 'migration-topic-123'
            AND remote_comment_id IS NULL
            AND remote_status = 0
            AND completed_at IS NOT NULL
          WHEN 'translation' THEN
            status = 'uncertain'
            AND attempt_count = 2
            AND remote_topic_id IS NULL
            AND remote_comment_id IS NULL
            AND remote_status IS NULL
            AND error_message = 'Migration test uncertain outcome'
          WHEN 'source' THEN
            status = 'skipped'
            AND markdown_snapshot IS NULL
            AND content_sha256 IS NULL
            AND completed_at IS NOT NULL
        END
      )
    )::integer
    FROM bbs_publication_steps
    WHERE video_analysis_id = '00000000-0000-0000-0000-000000000012'
      AND target_key = 'portal-push:Youtube';"
)"
[ "$publication_steps_valid" = "1" ]

generated_hash_column_valid="$(
  "${PSQL_TEST[@]}" --tuples-only --no-align --command \
    "SELECT (is_generated = 'ALWAYS')::integer
     FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name = 'bbs_publication_steps'
       AND column_name = 'content_sha256';"
)"
[ "$generated_hash_column_valid" = "1" ]

automatic_index_valid="$(
  "${PSQL_TEST[@]}" --tuples-only --no-align --command \
    "SELECT (
      indexdef LIKE '%WHERE%'
      AND indexdef LIKE '%pending%'
      AND indexdef LIKE '%claimed%'
      AND indexdef LIKE '%in_progress%'
      AND indexdef LIKE '%created%'
      AND indexdef LIKE '%failed%'
      AND indexdef NOT LIKE '%uncertain%'
      AND indexdef NOT LIKE '%succeeded%'
      AND indexdef NOT LIKE '%skipped%'
    )::integer
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND indexname = 'idx_bbs_publication_steps_automatic';"
)"
[ "$automatic_index_valid" = "1" ]

assert_sql_rejected "Incomplete portal target was not rejected" <<'SQL'
INSERT INTO bbs_publication_steps(
  video_analysis_id,
  target_key,
  step,
  topic_title,
  markdown_snapshot,
  request_metadata
)
VALUES (
  '00000000-0000-0000-0000-000000000012',
  'invalid-portal-target',
  'topic',
  'Invalid portal target',
  'Invalid portal target markdown',
  '{"portal_target": {"origin": "https://portal.test"}}'::jsonb
);
SQL

assert_sql_rejected "Claimed row with a write start time was not rejected" <<'SQL'
INSERT INTO bbs_publication_steps(
  video_analysis_id,
  target_key,
  step,
  topic_title,
  markdown_snapshot,
  status,
  attempt_count,
  request_metadata,
  started_at
)
VALUES (
  '00000000-0000-0000-0000-000000000012',
  'invalid-claimed-start',
  'topic',
  'Invalid claimed start',
  'Invalid claimed start markdown',
  'claimed',
  1,
  '{
    "portal_target": {
      "origin": "https://portal.test",
      "user_id": "migration-user-id",
      "category_id": 4,
      "category_name": "Youtube",
      "username": "migration-publisher"
    }
  }'::jsonb,
  now()
);
SQL

assert_sql_rejected "Bound portal target mutation was not rejected" <<'SQL'
UPDATE bbs_publication_steps
SET request_metadata = jsonb_set(
  request_metadata,
  '{portal_target,username}',
  '"different-publisher"'::jsonb
)
WHERE video_analysis_id = '00000000-0000-0000-0000-000000000012'
  AND target_key = 'portal-push:Youtube'
  AND step = 'topic';
SQL

assert_sql_rejected "Bound portal target removal was not rejected" <<'SQL'
UPDATE bbs_publication_steps
SET request_metadata = request_metadata - 'portal_target'
WHERE video_analysis_id = '00000000-0000-0000-0000-000000000012'
  AND target_key = 'portal-push:Youtube'
  AND step = 'topic';
SQL

assert_sql_rejected "Blank topic title was not rejected" <<'SQL'
INSERT INTO bbs_publication_steps(
  video_analysis_id, target_key, step, topic_title, markdown_snapshot
)
VALUES (
  '00000000-0000-0000-0000-000000000012',
  'invalid-title',
  'topic',
  '   ',
  'Topic markdown'
);
SQL

assert_sql_rejected "Overlong topic title was not rejected" <<'SQL'
INSERT INTO bbs_publication_steps(
  video_analysis_id, target_key, step, topic_title, markdown_snapshot
)
VALUES (
  '00000000-0000-0000-0000-000000000012',
  'invalid-title-length',
  'topic',
  repeat('x', 129),
  'Topic markdown'
);
SQL

assert_sql_rejected "Comment step topic title was not rejected" <<'SQL'
INSERT INTO bbs_publication_steps(
  video_analysis_id, target_key, step, topic_title, markdown_snapshot
)
VALUES (
  '00000000-0000-0000-0000-000000000012',
  'invalid-comment-title',
  'translation',
  'Comment title',
  'Translation markdown'
);
SQL

assert_sql_rejected "Blank non-skipped markdown was not rejected" <<'SQL'
INSERT INTO bbs_publication_steps(
  video_analysis_id, target_key, step, topic_title, markdown_snapshot
)
VALUES (
  '00000000-0000-0000-0000-000000000012',
  'invalid-markdown',
  'topic',
  'Invalid markdown topic',
  '   '
);
SQL

assert_sql_rejected "Pending row with attempt state was not rejected" <<'SQL'
INSERT INTO bbs_publication_steps(
  video_analysis_id,
  target_key,
  step,
  topic_title,
  markdown_snapshot,
  attempt_count,
  started_at
)
VALUES (
  '00000000-0000-0000-0000-000000000012',
  'invalid-pending',
  'topic',
  'Invalid pending topic',
  'Pending markdown',
  1,
  now()
);
SQL

assert_sql_rejected "Succeeded topic without a remote topic id was not rejected" <<'SQL'
INSERT INTO bbs_publication_steps(
  video_analysis_id,
  target_key,
  step,
  topic_title,
  markdown_snapshot,
  status,
  attempt_count,
  started_at,
  completed_at
)
VALUES (
  '00000000-0000-0000-0000-000000000012',
  'invalid-succeeded',
  'topic',
  'Invalid succeeded topic',
  'Succeeded markdown',
  'succeeded',
  1,
  now(),
  now()
);
SQL

assert_sql_rejected "Topic row with a remote comment id was not rejected" <<'SQL'
INSERT INTO bbs_publication_steps(
  video_analysis_id,
  target_key,
  step,
  topic_title,
  markdown_snapshot,
  status,
  remote_topic_id,
  remote_comment_id,
  attempt_count,
  started_at,
  completed_at
)
VALUES (
  '00000000-0000-0000-0000-000000000012',
  'invalid-remote-id',
  'topic',
  'Invalid remote id topic',
  'Remote id markdown',
  'succeeded',
  'valid-topic-id',
  123,
  1,
  now(),
  now()
);
SQL

assert_sql_rejected "Negative remote object status was not rejected" <<'SQL'
INSERT INTO bbs_publication_steps(
  video_analysis_id,
  target_key,
  step,
  topic_title,
  markdown_snapshot,
  status,
  remote_topic_id,
  remote_status,
  attempt_count,
  started_at
)
VALUES (
  '00000000-0000-0000-0000-000000000012',
  'invalid-remote-status',
  'topic',
  'Invalid remote status topic',
  'Remote status markdown',
  'created',
  'invalid-status-topic',
  -1,
  1,
  now()
);
SQL

assert_sql_rejected "Non-source skipped row was not rejected" <<'SQL'
INSERT INTO bbs_publication_steps(
  video_analysis_id,
  target_key,
  step,
  topic_title,
  markdown_snapshot,
  status,
  completed_at
)
VALUES (
  '00000000-0000-0000-0000-000000000012',
  'invalid-skipped',
  'topic',
  'Invalid skipped topic',
  NULL,
  'skipped',
  now()
);
SQL

assert_sql_rejected "Skipped source with publication fields was not rejected" <<'SQL'
INSERT INTO bbs_publication_steps(
  video_analysis_id,
  target_key,
  step,
  markdown_snapshot,
  status,
  remote_comment_id,
  error_message,
  completed_at
)
VALUES (
  '00000000-0000-0000-0000-000000000012',
  'invalid-skipped-fields',
  'source',
  'Source markdown',
  'skipped',
  123,
  'Unexpected error',
  now()
);
SQL

assert_sql_rejected "Negative attempt count was not rejected" <<'SQL'
INSERT INTO bbs_publication_steps(
  video_analysis_id, target_key, step, topic_title, markdown_snapshot, attempt_count
)
VALUES (
  '00000000-0000-0000-0000-000000000012',
  'invalid-attempt',
  'topic',
  'Invalid attempt topic',
  'Attempt markdown',
  -1
);
SQL

assert_sql_rejected "Publication snapshot mutation was not rejected" <<'SQL'
UPDATE bbs_publication_steps
SET markdown_snapshot = 'Mutated markdown'
WHERE video_analysis_id = '00000000-0000-0000-0000-000000000012'
  AND target_key = 'portal-push:Youtube'
  AND step = 'topic';
SQL

assert_sql_rejected "Publication-bearing analysis deletion was not restricted" <<'SQL'
DELETE FROM video_analyses
WHERE id = '00000000-0000-0000-0000-000000000012';
SQL

# A failing migration must roll back its DDL and must not enter the ledger.
TEMP_ROOT="$(mktemp -d)"
cp -a "$ROOT_DIR/db" "$TEMP_ROOT/db"
printf '%s\n' \
  'CREATE TABLE rollback_probe(id integer);' \
  'SELECT 1 / 0;' \
  > "$TEMP_ROOT/db/migrations/999_failure.sql"

rollback_status=0
ENV_FILE=/dev/null "$TEMP_ROOT/db/migrate.sh" >/dev/null 2>&1 || rollback_status=$?
if [ "$rollback_status" -eq 0 ]; then
  echo "Failing migration unexpectedly succeeded" >&2
  exit 1
fi

rollback_clean="$(
  "${PSQL_TEST[@]}" --tuples-only --no-align --command \
    "SELECT (to_regclass('public.rollback_probe') IS NULL AND NOT EXISTS (SELECT 1 FROM schema_migrations WHERE version = '999_failure.sql'))::integer;"
)"
[ "$rollback_clean" = "1" ]

# Once recorded, migration contents are immutable.
printf '\n-- checksum mutation used by the integration test --\n' \
  >> "$TEMP_ROOT/db/migrations/001_init.sql"
checksum_status=0
checksum_output="$(ENV_FILE=/dev/null "$TEMP_ROOT/db/migrate.sh" 2>&1)" || checksum_status=$?
if [ "$checksum_status" -eq 0 ]; then
  echo "Modified migration unexpectedly passed checksum validation" >&2
  exit 1
fi

case "$checksum_output" in
  *"Migration checksum mismatch: 001_init.sql"*) ;;
  *)
    echo "Modified migration failed for an unexpected reason" >&2
    exit 1
    ;;
esac

echo "Migration integration test passed"
