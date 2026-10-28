import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants.js";

const backend = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const baseConfig: NextConfig = {
  // Standalone tracing creates symlinks (needs Developer Mode on Windows); enable it only in the
  // Linux container build: NEXT_OUTPUT=standalone pnpm build.
  output: process.env.NEXT_OUTPUT === "standalone" ? "standalone" : undefined,
  poweredByHeader: false,
  reactStrictMode: true,
  // Compression buffers proxied responses and would stall the SSE stream.
  compress: false,
  async rewrites() {
    // Same-origin API proxy: auth cookies stay first-party (SameSite=Lax) and no CORS is needed.
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

// `next dev` writes to .next-dev and `next build` to .next, so a production build can never
// corrupt the cache of a running dev server.
export default function config(phase: string): NextConfig {
  return { ...baseConfig, distDir: phase === PHASE_DEVELOPMENT_SERVER ? ".next-dev" : ".next" };
}
