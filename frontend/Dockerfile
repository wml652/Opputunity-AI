# syntax=docker/dockerfile:1.7
# Console image: Next.js standalone server. The API is proxied same-origin via BACKEND_URL.
# Build:  docker build -t ootg-web frontend
FROM node:20-bookworm-slim AS deps
RUN corepack enable
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store pnpm install --frozen-lockfile

FROM node:20-bookworm-slim AS build
RUN corepack enable
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1 NEXT_OUTPUT=standalone
RUN mkdir -p public && pnpm build

FROM node:20-bookworm-slim
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
RUN groupadd --system app && useradd --system --gid app app
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static
COPY --from=build --chown=app:app /app/public ./public
USER app
EXPOSE 3000
# BACKEND_URL (e.g. http://api:8000) is read at build time for rewrites; set it as a build arg
# or bake the production URL into the image.
CMD ["node", "server.js"]
