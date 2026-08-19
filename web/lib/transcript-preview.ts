export const TRANSCRIPT_PREVIEW_TARGET_LENGTH = 200;
export const TRANSCRIPT_PREVIEW_MAX_LENGTH = 800;
export const TRANSCRIPT_PREVIEW_ELLIPSIS = "…";

const PARAGRAPH_SEPARATOR = /(?:[\r\n\u2028\u2029\t]|\s{2,})+/u;
const PARAGRAPH_JOINER = "\n";

export interface TranscriptPreview {
  text: string;
  isTruncated: boolean;
}

export function buildTranscriptPreview(text: string): TranscriptPreview {
  const paragraphs = text
    .split(PARAGRAPH_SEPARATOR)
    .map((paragraph) => paragraph.trim())
    .filter(Boolean);

  if (paragraphs.length === 0) {
    return { text: "", isTruncated: false };
  }

  const selectedParagraphs: string[] = [];
  let selectedLength = 0;
  let nextParagraphIndex = 0;

  while (
    nextParagraphIndex < paragraphs.length &&
    (selectedParagraphs.length === 0 || selectedLength < TRANSCRIPT_PREVIEW_TARGET_LENGTH)
  ) {
    const paragraph = paragraphs[nextParagraphIndex];
    selectedParagraphs.push(paragraph);
    selectedLength +=
      Array.from(paragraph).length + (selectedParagraphs.length > 1 ? 1 : 0);
    nextParagraphIndex += 1;
  }

  const characters = Array.from(selectedParagraphs.join(PARAGRAPH_JOINER));
  const isTruncated =
    characters.length > TRANSCRIPT_PREVIEW_MAX_LENGTH ||
    nextParagraphIndex < paragraphs.length;
  const contentLimit =
    TRANSCRIPT_PREVIEW_MAX_LENGTH -
    (isTruncated ? Array.from(TRANSCRIPT_PREVIEW_ELLIPSIS).length : 0);

  return {
    text: characters.slice(0, contentLimit).join(""),
    isTruncated,
  };
}
