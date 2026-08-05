import type { ResolvedChannel } from "./types";

export const MAX_CHANNEL_REFERENCE_LENGTH = 512;

const YOUTUBE_CHANNEL_ID = /^UC[A-Za-z0-9_-]{22}$/;
const YOUTUBE_HOSTS = new Set(["youtube.com", "www.youtube.com", "m.youtube.com"]);

type InspectionPayload = {
  youtube_channel_id?: unknown;
  title?: unknown;
  channel_url?: unknown;
  handle?: unknown;
  description?: unknown;
  avatar_url?: unknown;
};

export function parseChannelReference(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }

  const normalized = value.trim();
  if (
    !normalized ||
    Array.from(normalized).length > MAX_CHANNEL_REFERENCE_LENGTH ||
    Array.from(normalized).some((character) => {
      const codePoint = character.codePointAt(0) ?? 0;
      return codePoint < 32 || codePoint === 127;
    })
  ) {
    return null;
  }
  return normalized;
}

export function parseChannelInspectionPayload(value: unknown): ResolvedChannel | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }

  const payload = value as InspectionPayload;
  const youtubeChannelId = requiredString(payload.youtube_channel_id);
  const title = requiredString(payload.title);
  const url = youtubeChannelUrl(payload.channel_url);
  const handle = optionalString(payload.handle);
  const description = optionalString(payload.description);
  const avatarUrl = optionalHttpUrl(payload.avatar_url);

  if (
    !youtubeChannelId ||
    !YOUTUBE_CHANNEL_ID.test(youtubeChannelId) ||
    !title ||
    !url ||
    handle === undefined ||
    description === undefined ||
    avatarUrl === undefined
  ) {
    return null;
  }

  return {
    youtubeChannelId,
    title,
    url,
    handle,
    description,
    avatarUrl,
  };
}

function requiredString(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  return value.trim() || null;
}

function optionalString(value: unknown): string | null | undefined {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value !== "string") {
    return undefined;
  }
  return value.trim() || null;
}

function youtubeChannelUrl(value: unknown): string | null {
  const candidate = requiredString(value);
  if (!candidate) {
    return null;
  }
  try {
    const url = new URL(candidate);
    return url.protocol === "https:" && YOUTUBE_HOSTS.has(url.hostname.toLowerCase())
      ? candidate
      : null;
  } catch {
    return null;
  }
}

function optionalHttpUrl(value: unknown): string | null | undefined {
  const candidate = optionalString(value);
  if (candidate === undefined || candidate === null) {
    return candidate;
  }
  try {
    const url = new URL(candidate);
    return url.protocol === "https:" || url.protocol === "http:" ? candidate : undefined;
  } catch {
    return undefined;
  }
}
