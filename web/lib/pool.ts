import "server-only";

import path from "node:path";

import { loadEnvConfig } from "@next/env";
import { Pool, type PoolConfig } from "pg";

const PROJECT_ROOT = path.resolve(process.cwd(), "..");
const POSTGRES_ENV_NAMES = [
  "POSTGRES_HOST",
  "POSTGRES_PORT",
  "POSTGRES_USER",
  "POSTGRES_PASSWORD",
  "POSTGRES_DB",
  "CHANNEL_ADMIN_POSTGRES_USER",
  "CHANNEL_ADMIN_POSTGRES_PASSWORD",
] as const;

type PoolGlobal = typeof globalThis & {
  __youtubeFetchReadOnlyPool?: Pool;
  __youtubeFetchChannelManagementPool?: Pool;
};

const poolGlobal = globalThis as PoolGlobal;
let productionReadOnlyPool: Pool | undefined;
let productionChannelManagementPool: Pool | undefined;

function requiredEnv(name: string, trim = true): string {
  const value = process.env[name];
  const normalized = trim ? value?.trim() : value;
  if (!normalized) {
    throw new Error(`${name} is required in the process environment or root .env`);
  }
  return normalized;
}

function loadDatabaseEnvironment(): void {
  const processOverrides = new Map(
    POSTGRES_ENV_NAMES.flatMap((name) =>
      Object.hasOwn(process.env, name) ? [[name, process.env[name]]] : [],
    ),
  );
  loadEnvConfig(
    PROJECT_ROOT,
    process.env.NODE_ENV !== "production",
    console,
    true,
  );
  for (const [name, value] of processOverrides) {
    if (value === undefined) {
      delete process.env[name];
    } else {
      process.env[name] = value;
    }
  }
}

function databaseConfig(
  access: "read-only" | "channel-management",
): PoolConfig {
  loadDatabaseEnvironment();

  const rawPort = requiredEnv("POSTGRES_PORT");
  const port = Number(rawPort);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error("POSTGRES_PORT must be an integer between 1 and 65535");
  }

  const managementUser = process.env.CHANNEL_ADMIN_POSTGRES_USER?.trim();
  const managementPassword = process.env.CHANNEL_ADMIN_POSTGRES_PASSWORD;
  if (Boolean(managementUser) !== Boolean(managementPassword)) {
    throw new Error(
      "CHANNEL_ADMIN_POSTGRES_USER and CHANNEL_ADMIN_POSTGRES_PASSWORD must be configured together",
    );
  }

  return {
    host: requiredEnv("POSTGRES_HOST"),
    port,
    user:
      access === "channel-management" && managementUser
        ? managementUser
        : requiredEnv("POSTGRES_USER"),
    password:
      access === "channel-management" && managementPassword
        ? managementPassword
        : requiredEnv("POSTGRES_PASSWORD", false),
    database: requiredEnv("POSTGRES_DB"),
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

function createPool(access: "read-only" | "channel-management"): Pool {
  const pool = new Pool(databaseConfig(access));
  pool.on("error", (error) => {
    console.error(`Unexpected idle PostgreSQL ${access} client error`, error);
  });
  return pool;
}

export function getReadOnlyPool(): Pool {
  if (process.env.NODE_ENV !== "production") {
    poolGlobal.__youtubeFetchReadOnlyPool ??= createPool("read-only");
    return poolGlobal.__youtubeFetchReadOnlyPool;
  }

  productionReadOnlyPool ??= createPool("read-only");
  return productionReadOnlyPool;
}

export function getChannelManagementPool(): Pool {
  if (process.env.NODE_ENV !== "production") {
    poolGlobal.__youtubeFetchChannelManagementPool ??= createPool(
      "channel-management",
    );
    return poolGlobal.__youtubeFetchChannelManagementPool;
  }

  productionChannelManagementPool ??= createPool("channel-management");
  return productionChannelManagementPool;
}
