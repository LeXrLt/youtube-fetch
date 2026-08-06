import { type NextRequest, NextResponse } from "next/server";

// Keep this Proxy matcher-free: Next prefixes explicit matchers with basePath.
export function proxy(request: NextRequest) {
  if (!request.nextUrl.basePath) {
    return new NextResponse(null, { status: 404 });
  }

  return NextResponse.next();
}
