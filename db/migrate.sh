#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
MIGRATIONS_DIR="$ROOT_DIR/db/migrations"
POSTGRES_VARIABLES=(
  POSTGRES_HOST
  POSTGRES_PORT
  POSTGRES_USER
  POSTGRES_PASSWORD
  POSTGRES_DB
)

# Process environment values take precedence over the local dotenv file.
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
: "${POSTGRES_DB:?POSTGRES_DB is required in the environment or $ENV_FILE}"

for command_name in psql sha256sum; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "$command_name is required to run database migrations" >&2
    exit 1
  fi
done

if [ ! -d "$MIGRATIONS_DIR" ]; then
  echo "Missing migrations directory at $MIGRATIONS_DIR" >&2
  exit 1
fi

export PGPASSWORD="$POSTGRES_PASSWORD"

PSQL_BASE=(
  psql
  --no-psqlrc
  --host "$POSTGRES_HOST"
  --port "$POSTGRES_PORT"
  --username "$POSTGRES_USER"
  --no-password
  --set ON_ERROR_STOP=1
)

database_exists() {
  printf '%s\n' "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database_name')::integer;" |
    "${PSQL_BASE[@]}" \
      --dbname postgres \
      --tuples-only \
      --no-align \
      --set "database_name=$POSTGRES_DB"
}

if [ "$(database_exists)" != "1" ]; then
  if ! command -v createdb >/dev/null 2>&1; then
    echo "createdb is required to create database $POSTGRES_DB" >&2
    exit 1
  fi

  if ! create_error="$(
    createdb \
      --host "$POSTGRES_HOST" \
      --port "$POSTGRES_PORT" \
      --username "$POSTGRES_USER" \
      --no-password \
      "$POSTGRES_DB" 2>&1
  )" && [ "$(database_exists)" != "1" ]; then
    echo "$create_error" >&2
    exit 1
  fi
fi

# The transaction lock serializes migration metadata setup across deploy processes.
{
  printf 'BEGIN;\n'
  printf "SELECT pg_advisory_xact_lock(hashtextextended(current_database() || ':schema_migrations', 0));\n"
  printf '%s\n' "CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, checksum text, applied_at timestamptz NOT NULL DEFAULT now());"
  printf '%s\n' "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum text;"
  printf 'COMMIT;\n'
} | "${PSQL_BASE[@]}" --dbname "$POSTGRES_DB"

recorded_versions="$(
  "${PSQL_BASE[@]}" \
    --dbname "$POSTGRES_DB" \
    --tuples-only \
    --no-align \
    --command "SELECT version FROM schema_migrations ORDER BY version;"
)"

while IFS= read -r version; do
  [ -z "$version" ] && continue
  if [ ! -f "$MIGRATIONS_DIR/$version" ]; then
    echo "Applied migration is missing from disk: $version" >&2
    exit 1
  fi
done <<< "$recorded_versions"

for migration in "$MIGRATIONS_DIR"/*.sql; do
  [ -e "$migration" ] || continue

  version="$(basename "$migration")"
  if [[ ! "$version" =~ ^[0-9]{3}_[a-z0-9_]+\.sql$ ]]; then
    echo "Invalid migration filename: $version" >&2
    echo "Expected format: NNN_lowercase_name.sql" >&2
    exit 1
  fi

  checksum="$(sha256sum "$migration")"
  checksum="${checksum%% *}"

  {
    printf 'BEGIN;\n'
    printf "SELECT pg_advisory_xact_lock(hashtextextended(current_database() || ':schema_migrations', 0));\n"
    printf "UPDATE schema_migrations SET checksum = '%s' WHERE version = '%s' AND checksum IS NULL;\n" "$checksum" "$version"
    printf "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = '%s') AS migration_applied,\n" "$version"
    printf "       COALESCE((SELECT checksum = '%s' FROM schema_migrations WHERE version = '%s'), true) AS checksum_matches\n" "$checksum" "$version"
    printf '\\gset\n'
    printf '\\if :checksum_matches\n'
    printf '\\else\n'
    printf '\\echo Migration checksum mismatch: %s\n' "$version"
    printf '\\quit 3\n'
    printf '\\endif\n'
    printf '\\if :migration_applied\n'
    printf '\\echo Skipping %s\n' "$version"
    printf '\\else\n'
    command cat "$migration"
    printf "\nINSERT INTO schema_migrations(version, checksum) VALUES ('%s', '%s');\n" "$version" "$checksum"
    printf '\\endif\n'
    printf 'COMMIT;\n'
  } | "${PSQL_BASE[@]}" --dbname "$POSTGRES_DB"
done

{
  printf 'BEGIN;\n'
  printf "SELECT pg_advisory_xact_lock(hashtextextended(current_database() || ':schema_migrations', 0));\n"
  printf '%s\n' "ALTER TABLE schema_migrations ALTER COLUMN checksum SET NOT NULL;"
  printf 'COMMIT;\n'
} | "${PSQL_BASE[@]}" --dbname "$POSTGRES_DB"
