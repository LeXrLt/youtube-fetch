import { describe, expect, it } from "vitest";

import {
  TRANSCRIPT_PREVIEW_LENGTH,
  buildTranscriptPreview,
} from "./transcript-preview";

describe("buildTranscriptPreview", () => {
  it.each([99, 100])("keeps a %i-character transcript intact", (length) => {
    const text = "字".repeat(length);

    expect(buildTranscriptPreview(text)).toEqual({ text, isTruncated: false });
  });

  it("returns the first 100 characters for a longer transcript", () => {
    const text = `${"前".repeat(TRANSCRIPT_PREVIEW_LENGTH)}后`;

    expect(buildTranscriptPreview(text)).toEqual({
      text: "前".repeat(TRANSCRIPT_PREVIEW_LENGTH),
      isTruncated: true,
    });
  });

  it("counts Unicode code points without splitting an emoji", () => {
    const text = `${"a".repeat(99)}😀结束`;
    const preview = buildTranscriptPreview(text);

    expect(Array.from(preview.text)).toHaveLength(TRANSCRIPT_PREVIEW_LENGTH);
    expect(preview.text.endsWith("😀")).toBe(true);
    expect(preview.isTruncated).toBe(true);
  });

  it("collapses subtitle line breaks and repeated whitespace", () => {
    expect(buildTranscriptPreview("  第一行\n\n第二行\t  结束  ")).toEqual({
      text: "第一行 第二行 结束",
      isTruncated: false,
    });
  });
});
