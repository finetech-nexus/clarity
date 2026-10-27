/** @type {import('next').NextConfig} */
const upstream = process.env.CLARITY_API_UPSTREAM || "";
const nextConfig = {
  reactStrictMode: true,
  transpilePackages: ["keycloak-js"],
  experimental: { proxyTimeout: 180_000 },
  async rewrites() {
    if (!upstream) return [];
    const prefixes = [
      "api",
      "v1",
      "v1beta",
      "ingestion",
      "scenarios",
      "scenario-iterations",
      "scenario-iteration-rules",
      "data-model",
      "settings",
      "custom-lists",
      "config",
      "cases",
    ];
    return prefixes.flatMap((prefix) => [
      { source: `/${prefix}`, destination: `${upstream}/${prefix}` },
      { source: `/${prefix}/:path*`, destination: `${upstream}/${prefix}/:path*` },
    ]);
  },
};
module.exports = nextConfig;
