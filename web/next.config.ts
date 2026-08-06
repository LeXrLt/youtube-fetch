import type { NextConfig } from "next";

import { getWebBasePath } from "./lib/web-route";

const nextConfig: NextConfig = {
  basePath: getWebBasePath(),
};

export default nextConfig;
