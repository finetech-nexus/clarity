/** @type {import('next').NextConfig} */
const upstream = process.env.CLARITY_API_UPSTREAM || "";
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    if (!upstream) return [];
    return ["api", "v1", "v1beta", "ingestion"].map((prefix) => ({
      source: `/${prefix}/:path*`,
      destination: `${upstream}/${prefix}/:path*`,
    }));
  },
};
module.exports = nextConfig;
