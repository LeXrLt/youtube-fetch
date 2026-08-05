import "server-only";

import { Pool } from "pg";

import {
  buildDatabaseConfig,
  type DatabaseAccess,
} from "./database-config";

type PoolGlobal = typeof globalThis & {
  __youtubeFetchReadOnlyPool?: Pool;
  __youtubeFetchChannelManagementPool?: Pool;
};

const poolGlobal = globalThis as PoolGlobal;
let productionReadOnlyPool: Pool | undefined;
let productionChannelManagementPool: Pool | undefined;

function createPool(access: DatabaseAccess): Pool {
  const pool = new Pool(buildDatabaseConfig(access, process.env.DATABASE_URL));
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
