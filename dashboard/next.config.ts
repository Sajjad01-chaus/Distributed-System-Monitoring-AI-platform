import type { NextConfig } from "next";

// Static export: the dashboard is plain HTML/JS that talks to the API from the browser,
// so it can be hosted on Vercel, any CDN, or behind the same nginx.
const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
};

export default nextConfig;
