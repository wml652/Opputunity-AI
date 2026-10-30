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
};

// `next dev` writes to .next-dev and `next build` to .next, so a production build can never
// corrupt the cache of a running dev server.
export default function config(phase: string): NextConfig {
  const dev = phase === PHASE_DEVELOPMENT_SERVER;
  return { ...baseConfig, distDir: dev ? ".next-dev" : ".next", headers: securityHeaders(dev) };
}

/** Console security headers. The CSP allows only same-origin resources: the API is proxied
 * same-origin, fonts are self-hosted by next/font, screenshots are same-origin images. Next.js
 * hydration needs inline scripts; the dev server additionally needs eval + its HMR websocket. */
function securityHeaders(dev: boolean): NonNullable<NextConfig["headers"]> {
  const csp = [
    "default-src 'self'",
    `script-src 'self' 'unsafe-inline'${dev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    `connect-src 'self'${dev ? " ws: wss:" : ""}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");
  const headers = [
    { key: "Content-Security-Policy", value: csp },
    { key: "X-Content-Type-Options", value: "nosniff" },
    { key: "X-Frame-Options", value: "DENY" },
    { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
    { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
    { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
  ];
  if (!dev) {
    headers.push({ key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" });
  }
  return async () => [{ source: "/:path*", headers }];
}
