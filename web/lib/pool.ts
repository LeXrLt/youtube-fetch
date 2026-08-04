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
] as const;

type PoolGlobal = typeof globalThis & {
  __youtubeFetchReadOnlyPool?: Pool;
};

const poolGlobal = globalThis as PoolGlobal;
let productionPool: Pool | undefined;

function requiredEnv(name: string, trim = true): string {
  const value = process.env[name];
  const normalized = trim ? value?.trim() : value;
  if (!normalized) {
    throw new Error(`${name} is required in the process environment or root .env`);
  }
  return normalized;
}

function databaseConfig(): PoolConfig {
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

  const rawPort = requiredEnv("POSTGRES_PORT");
  const port = Number(rawPort);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error("POSTGRES_PORT must be an integer between 1 and 65535");
  }

  return {
    host: requiredEnv("POSTGRES_HOST"),
    port,
    user: requiredEnv("POSTGRES_USER"),
    password: requiredEnv("POSTGRES_PASSWORD", false),
    database: requiredEnv("POSTGRES_DB"),
    application_name: "youtube-fetch-web",
    options: "-c default_transaction_read_only=on",
    max: 5,
    min: 0,
    connectionTimeoutMillis: 10_000,
    idleTimeoutMillis: 30_000,
    statement_timeout: 15_000,
    query_timeout: 20_000,
    allowExitOnIdle: true,
  };
}

function createPool(): Pool {
  const pool = new Pool(databaseConfig());
  pool.on("error", (error) => {
    console.error("Unexpected idle PostgreSQL client error", error);
  });
  return pool;
}

export function getReadOnlyPool(): Pool {
  if (process.env.NODE_ENV !== "production") {
    poolGlobal.__youtubeFetchReadOnlyPool ??= createPool();
    return poolGlobal.__youtubeFetchReadOnlyPool;
  }

  productionPool ??= createPool();
  return productionPool;
}
