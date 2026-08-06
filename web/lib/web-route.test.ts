import { describe, expect, it } from "vitest";

import { getWebBasePath, getWebRouteKey, withWebBasePath } from "./web-route";

const ROUTE_KEY = "0123456789abcdef0123456789abcdef";

describe("Web route key", () => {
  it("builds the protected base path", () => {
    expect(getWebRouteKey(ROUTE_KEY)).toBe(ROUTE_KEY);
    expect(getWebBasePath(ROUTE_KEY)).toBe(`/${ROUTE_KEY}`);
    expect(withWebBasePath("/", ROUTE_KEY)).toBe(`/${ROUTE_KEY}`);
    expect(withWebBasePath("/tags/example", ROUTE_KEY)).toBe(
      `/${ROUTE_KEY}/tags/example`,
    );
  });

  it.each([
    undefined,
    "",
    "short",
    "0123456789abcdef0123456789abcdeG",
    "0123456789ABCDEF0123456789ABCDEF",
    "0123456789abcdef0123456789abcdef0",
  ])("rejects an invalid key (%s)", (routeKey) => {
    expect(() => getWebRouteKey(routeKey)).toThrow(/WEB_ROUTE_KEY/);
  });

  it.each(["tags", "https://example.test/tags", "//example.test/tags"])(
    "rejects a non-application pathname (%s)",
    (pathname) => {
      expect(() => withWebBasePath(pathname, ROUTE_KEY)).toThrow(
        "pathname must be an absolute application path",
      );
    },
  );
});
