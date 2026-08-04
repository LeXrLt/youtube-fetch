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

table_count="$(
  "${PSQL_TEST[@]}" --tuples-only --no-align --command \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('schema_migrations', 'researchers', 'youtube_channels', 'videos', 'subtitle_tracks', 'analysis_runs', 'agent_invocations', 'video_analyses', 'tags', 'video_analysis_tags');"
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

[ "$table_count" = "10" ]
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
SQL

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
INSERT INTO video_analyses(video_id, subtitle_track_id)
VALUES (
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
