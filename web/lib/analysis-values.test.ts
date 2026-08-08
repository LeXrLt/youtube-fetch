import { describe, expect, it } from "vitest";

import { analysisKeyPoints, analysisSources } from "./analysis-values";

describe("analysisKeyPoints", () => {
  it("keeps string points and renders valid JSON values without failing the page", () => {
    expect(
      analysisKeyPoints(["结论", { evidence: [1, 2] }, true, null, "  "]),
    ).toEqual(["结论", '{"evidence":[1,2]}', "true", "null"]);
  });

  it("returns an empty list for a non-array projection", () => {
    expect(analysisKeyPoints({ point: "结论" })).toEqual([]);
  });
});

describe("analysisSources", () => {
  it("keeps valid source entries and tolerates a missing note", () => {
    expect(
      analysisSources([
        { title: "文档", url: "https://example.com", note: "背景" },
        { title: "补充", url: "https://example.org" },
      ]),
    ).toEqual([
      { title: "文档", url: "https://example.com", note: "背景" },
      { title: "补充", url: "https://example.org", note: "" },
    ]);
  });

  it("drops incompatible entries instead of failing the detail page", () => {
    expect(
      analysisSources([
        null,
        { title: "缺少地址" },
        { title: 1, url: "https://example.com" },
      ]),
    ).toEqual([]);
    expect(analysisSources({})).toEqual([]);
  });
});
