import { describe, expect, it } from "vitest";

import {
  TRANSCRIPT_PREVIEW_ELLIPSIS,
  TRANSCRIPT_PREVIEW_MAX_LENGTH,
  TRANSCRIPT_PREVIEW_TARGET_LENGTH,
  buildTranscriptPreview,
} from "./transcript-preview";

describe("buildTranscriptPreview", () => {
  it("keeps a complete first paragraph once it reaches the target length", () => {
    const firstParagraph = "首".repeat(350);
    const text = `${firstParagraph}\nsecond paragraph`;

    expect(buildTranscriptPreview(text)).toEqual({
      text: firstParagraph,
      isTruncated: true,
    });
  });

  it("adds complete paragraphs until the preview reaches 200 characters", () => {
    const firstParagraph = "一".repeat(100);
    const secondParagraph = "二".repeat(50);
    const thirdParagraph = "三".repeat(60);
    const text = `${firstParagraph}\n${secondParagraph}\t${thirdParagraph}  fourth`;

    expect(buildTranscriptPreview(text)).toEqual({
      text: `${firstParagraph}\n${secondParagraph}\n${thirdParagraph}`,
      isTruncated: true,
    });
  });

  it("does not add another paragraph when the first one is exactly 200 characters", () => {
    const firstParagraph = "一".repeat(TRANSCRIPT_PREVIEW_TARGET_LENGTH);

    expect(buildTranscriptPreview(`${firstParagraph}\nsecond paragraph`)).toEqual({
      text: firstParagraph,
      isTruncated: true,
    });
  });

  it("includes all remaining paragraphs when their total stays below 200 characters", () => {
    expect(buildTranscriptPreview("first paragraph\nsecond\tthird  fourth")).toEqual({
      text: "first paragraph\nsecond\nthird\nfourth",
      isTruncated: false,
    });
  });

  it("uses line breaks, tabs, and repeated whitespace as paragraph separators", () => {
    expect(buildTranscriptPreview("  first line\r\nsecond\tthird   fourth  ")).toEqual({
      text: "first line\nsecond\nthird\nfourth",
      isTruncated: false,
    });
  });

  it("preserves single spaces within a paragraph", () => {
    expect(buildTranscriptPreview("  first paragraph keeps spaces  ")).toEqual({
      text: "first paragraph keeps spaces",
      isTruncated: false,
    });
  });

  it("caps a long paragraph at 800 characters", () => {
    const text = `${"前".repeat(TRANSCRIPT_PREVIEW_MAX_LENGTH)}后`;

    expect(buildTranscriptPreview(text)).toEqual({
      text: "前".repeat(TRANSCRIPT_PREVIEW_MAX_LENGTH - 1),
      isTruncated: true,
    });
  });

  it("keeps an exactly 800-character transcript intact", () => {
    const text = "整".repeat(TRANSCRIPT_PREVIEW_MAX_LENGTH);

    expect(buildTranscriptPreview(text)).toEqual({
      text,
      isTruncated: false,
    });
  });

  it("caps an added paragraph when it takes the preview past 800 characters", () => {
    const firstParagraph = "一".repeat(100);
    const secondParagraph = "二".repeat(TRANSCRIPT_PREVIEW_MAX_LENGTH);
    const preview = buildTranscriptPreview(`${firstParagraph}\n${secondParagraph}`);

    expect(Array.from(preview.text + TRANSCRIPT_PREVIEW_ELLIPSIS)).toHaveLength(
      TRANSCRIPT_PREVIEW_MAX_LENGTH,
    );
    expect(preview.text).toBe(
      `${firstParagraph}\n${"二".repeat(TRANSCRIPT_PREVIEW_MAX_LENGTH - 102)}`,
    );
    expect(preview.isTruncated).toBe(true);
  });

  it("counts Unicode code points without splitting an emoji surrogate pair", () => {
    const text = `${"a".repeat(TRANSCRIPT_PREVIEW_MAX_LENGTH - 2)}😀结束`;
    const preview = buildTranscriptPreview(text);

    expect(Array.from(preview.text + TRANSCRIPT_PREVIEW_ELLIPSIS)).toHaveLength(
      TRANSCRIPT_PREVIEW_MAX_LENGTH,
    );
    expect(preview.text.endsWith("😀")).toBe(true);
    expect(preview.isTruncated).toBe(true);
  });

  it("returns an empty preview for whitespace-only content", () => {
    expect(buildTranscriptPreview(" \n\t   ")).toEqual({
      text: "",
      isTruncated: false,
    });
  });

  it("does not mark trailing paragraph separators as truncated content", () => {
    expect(buildTranscriptPreview("only paragraph\n\t   ")).toEqual({
      text: "only paragraph",
      isTruncated: false,
    });
  });

  it("uses the configured 200-character target", () => {
    const firstParagraph = "一".repeat(TRANSCRIPT_PREVIEW_TARGET_LENGTH - 1);
    const secondParagraph = "二";

    expect(buildTranscriptPreview(`${firstParagraph}\n${secondParagraph}`)).toEqual({
      text: `${firstParagraph}\n${secondParagraph}`,
      isTruncated: false,
    });
  });
});
