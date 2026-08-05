import { describe, expect, it } from "vitest";

import { buildDatabaseConfig } from "./database-config";

const DATABASE_URL =
  "postgresql://hub_user:hub_password@localhost:5432/youtube_fetch";

describe("buildDatabaseConfig", () => {
  it("uses DATABASE_URL and keeps display queries read-only", () => {
    const config = buildDatabaseConfig("read-only", DATABASE_URL);

    expect(config.connectionString).toBe(DATABASE_URL);
    expect(config.application_name).toBe("youtube-fetch-web");
    expect(config.options).toBe("-c default_transaction_read_only=on");
    expect(config.max).toBe(5);
  });

  it("uses the same DATABASE_URL for channel management writes", () => {
    const config = buildDatabaseConfig("channel-management", DATABASE_URL);

    expect(config.connectionString).toBe(DATABASE_URL);
    expect(config.application_name).toBe(
      "youtube-fetch-web-channel-management",
    );
    expect(config.options).toBeUndefined();
    expect(config.max).toBe(2);
  });

  it.each([undefined, "", "   "])(
    "rejects a missing DATABASE_URL value (%s)",
    (databaseUrl) => {
      expect(() => buildDatabaseConfig("read-only", databaseUrl)).toThrow(
        "DATABASE_URL is required in web/.env.local or the process environment",
      );
    },
  );

  it.each([
    "not-a-url",
    "https://hub_user:hub_password@localhost:5432/youtube_fetch",
    "postgresql://",
    "postgresql://hub_user@localhost:5432/youtube_fetch",
    "postgresql://hub_user:hub_password@localhost:5432",
  ])("rejects a non-PostgreSQL URL (%s)", (databaseUrl) => {
    expect(() => buildDatabaseConfig("read-only", databaseUrl)).toThrow(
      "DATABASE_URL must be a complete postgres:// or postgresql:// URL",
    );
  });

  it("rejects URL options that could disable the read-only session", () => {
    const databaseUrl = `${DATABASE_URL}?options=-c%20default_transaction_read_only%3Doff`;

    expect(() => buildDatabaseConfig("read-only", databaseUrl)).toThrow(
      "DATABASE_URL must not define options because the application controls session options",
    );
  });
});
