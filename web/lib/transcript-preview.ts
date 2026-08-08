export const TRANSCRIPT_PREVIEW_LENGTH = 100;

export interface TranscriptPreview {
  text: string;
  isTruncated: boolean;
}

export function buildTranscriptPreview(text: string): TranscriptPreview {
  const normalized = text.replace(/\s+/g, " ").trim();
  const characters = Array.from(normalized);
  if (characters.length <= TRANSCRIPT_PREVIEW_LENGTH) {
    return { text: normalized, isTruncated: false };
  }

  return {
    text: characters.slice(0, TRANSCRIPT_PREVIEW_LENGTH).join(""),
    isTruncated: true,
  };
}
