const WEB_ROUTE_KEY_PATTERN = /^[0-9a-f]{32}$/;

export function getWebRouteKey(value = process.env.WEB_ROUTE_KEY): string {
  if (!value) {
    throw new Error(
      "WEB_ROUTE_KEY is required in web/.env.local or the process environment",
    );
  }

  if (!WEB_ROUTE_KEY_PATTERN.test(value)) {
    throw new Error(
      "WEB_ROUTE_KEY must be exactly 32 lowercase hexadecimal characters",
    );
  }

  return value;
}

export function getWebBasePath(value = process.env.WEB_ROUTE_KEY): string {
  return `/${getWebRouteKey(value)}`;
}

export function withWebBasePath(
  pathname: string,
  value = process.env.WEB_ROUTE_KEY,
): string {
  if (!pathname.startsWith("/") || pathname.startsWith("//")) {
    throw new Error("pathname must be an absolute application path");
  }

  return `${getWebBasePath(value)}${pathname === "/" ? "" : pathname}`;
}
