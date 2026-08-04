import { describe, expect, it } from "vitest";

import { safeHttpUrl } from "./public-url";

describe("safeHttpUrl", () => {
  it.each([
    ["https://www.youtube.com/watch?v=abc", "https://www.youtube.com/watch?v=abc"],
    ["http://images.example.test/avatar.jpg", "http://images.example.test/avatar.jpg"],
    ["javascript:alert(1)", null],
    ["data:text/html,<script>alert(1)</script>", null],
    ["file:///etc/passwd", null],
    ["not a URL", null],
    [null, null],
  ])("maps %j to %j", (input, expected) => {
    expect(safeHttpUrl(input)).toBe(expected);
  });
});
