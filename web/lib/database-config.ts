import type { PoolConfig } from "pg";

export type DatabaseAccess = "read-only" | "channel-management";

function requiredDatabaseUrl(databaseUrl: string | undefined): string {
  const normalized = databaseUrl?.trim();
  if (!normalized) {
    throw new Error(
      "DATABASE_URL is required in web/.env.local or the process environment",
    );
  }

  let url: URL;
  try {
    url = new URL(normalized);
  } catch {
    throw new Error(
      "DATABASE_URL must be a complete postgres:// or postgresql:// URL",
    );
  }

  const hasPostgresProtocol =
    url.protocol === "postgres:" || url.protocol === "postgresql:";
  const hasRequiredComponents =
    Boolean(url.username) &&
    Boolean(url.password) &&
    Boolean(url.hostname) &&
    url.pathname.length > 1;
  if (!hasPostgresProtocol || !hasRequiredComponents) {
    throw new Error(
      "DATABASE_URL must be a complete postgres:// or postgresql:// URL",
    );
  }
  if (url.searchParams.has("options")) {
    throw new Error(
      "DATABASE_URL must not define options because the application controls session options",
    );
  }

  return normalized;
}

export function buildDatabaseConfig(
  access: DatabaseAccess,
  databaseUrl: string | undefined,
): PoolConfig {
  return {
    connectionString: requiredDatabaseUrl(databaseUrl),
    application_name:
      access === "read-only"
        ? "youtube-fetch-web"
        : "youtube-fetch-web-channel-management",
    options:
      access === "read-only" ? "-c default_transaction_read_only=on" : undefined,
    max: access === "read-only" ? 5 : 2,
    min: 0,
    connectionTimeoutMillis: 10_000,
    idleTimeoutMillis: 30_000,
    statement_timeout: 15_000,
    query_timeout: 20_000,
    allowExitOnIdle: true,
  };
}
