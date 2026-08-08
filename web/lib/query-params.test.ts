import { describe, expect, it } from "vitest";

import {
  MAX_PAGE,
  MAX_QUERY_LENGTH,
  PAGE_SIZE,
  buildLikePattern,
  isUuid,
  paginationOffset,
  parseFeedQuery,
  parsePage,
  parsePostMode,
  parseQuery,
} from "./query-params";

describe("parsePage", () => {
  it.each([
    [undefined, 1],
    [null, 1],
    ["", 1],
    ["0", 1],
    ["-2", 1],
    ["2.5", 1],
    ["2oops", 1],
    [Number.POSITIVE_INFINITY, 1],
    [" 7 ", 7],
    [3, 3],
    [["4", "9"], 4],
    [String(MAX_PAGE + 1), MAX_PAGE],
  ])("maps %j to %i", (input, expected) => {
    expect(parsePage(input)).toBe(expected);
  });
});

describe("parseQuery", () => {
  it("trims, selects the first repeated value, and bounds Unicode code points", () => {
    expect(parseQuery(["  postgres  ", "ignored"])).toBe("postgres");
    expect(parseQuery(42)).toBe("");

    const longQuery = "界".repeat(MAX_QUERY_LENGTH + 10);
    expect(Array.from(parseQuery(longQuery))).toHaveLength(MAX_QUERY_LENGTH);
  });

  it("removes NUL characters that PostgreSQL text values cannot encode", () => {
    expect(parseQuery("  post\0gres\0  ")).toBe("postgres");
    expect(parseQuery("\0")).toBe("");
  });

  it("normalizes a feed query in one call", () => {
    expect(parseFeedQuery({ q: "  agent ", page: "2" })).toEqual({
      q: "agent",
      page: 2,
    });
  });
});

describe("parsePostMode", () => {
  it("defaults missing and unsupported values to original", () => {
    expect(parsePostMode(undefined)).toBe("original");
    expect(parsePostMode("original")).toBe("original");
    expect(parsePostMode("invalid")).toBe("original");
  });

  it("accepts translated mode and selects the first repeated value", () => {
    expect(parsePostMode("translated")).toBe("translated");
    expect(parsePostMode(["translated", "original"])).toBe("translated");
  });
});

describe("SQL value helpers", () => {
  it("escapes LIKE metacharacters while retaining substring matching", () => {
    expect(buildLikePattern("100%_\\done")).toBe("%100\\%\\_\\\\done%");
  });

  it("calculates a bounded page offset", () => {
    expect(paginationOffset(1)).toBe(0);
    expect(paginationOffset(3)).toBe(PAGE_SIZE * 2);
  });

  it("accepts canonical PostgreSQL UUID text only", () => {
    expect(isUuid("28f5f390-62e7-4f27-a478-75d46c331b77")).toBe(true);
    expect(isUuid("00000000-0000-0000-0000-000000000000")).toBe(true);
    expect(isUuid("28f5f39062e74f27a47875d46c331b77")).toBe(false);
    expect(isUuid("not-a-uuid")).toBe(false);
  });
});
