import type { FeedMode, FeedQueryInput, SearchParamValue } from "./types";

export const PAGE_SIZE = 12;
export const MAX_PAGE = 10_000;
export const MAX_QUERY_LENGTH = 200;

export interface ParsedFeedQuery {
  page: number;
  q: string;
}

function firstValue(value: SearchParamValue): string | number | null | undefined {
  return Array.isArray(value) ? value[0] : value;
}

export function parsePage(value: SearchParamValue): number {
  const candidate = firstValue(value);
  let parsed: number;

  if (typeof candidate === "number") {
    parsed = candidate;
  } else if (typeof candidate === "string" && /^\d+$/.test(candidate.trim())) {
    parsed = Number(candidate.trim());
  } else {
    return 1;
  }

  if (!Number.isSafeInteger(parsed)) {
    return 1;
  }

  return Math.min(MAX_PAGE, Math.max(1, parsed));
}

export function parseQuery(value: SearchParamValue): string {
  const candidate = firstValue(value);
  if (typeof candidate !== "string") {
    return "";
  }

  const normalized = candidate.replaceAll("\0", "").trim();
  return Array.from(normalized).slice(0, MAX_QUERY_LENGTH).join("");
}

export function parsePostMode(value: SearchParamValue): FeedMode {
  return firstValue(value) === "translated" ? "translated" : "original";
}

export function parseFeedQuery(input: FeedQueryInput = {}): ParsedFeedQuery {
  return {
    page: parsePage(input.page),
    q: parseQuery(input.q),
  };
}

export function buildLikePattern(query: string): string {
  return `%${query.replace(/[\\%_]/g, "\\$&")}%`;
}

export function paginationOffset(page: number): number {
  return (page - 1) * PAGE_SIZE;
}

export function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
    value,
  );
}
