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
# The /api rewrite target is baked into the build (routes manifest): pass the API's address on
# the container network, e.g. --build-arg BACKEND_URL=http://ootg-api (Azure Container Apps
# internal ingress) or http://api:8000 (docker compose). WebSocket upgrades on /api (remote
# browser sessions) are proxied by the Next.js server to the same target.
ARG BACKEND_URL=http://api:8000
ENV NEXT_TELEMETRY_DISABLED=1 NEXT_OUTPUT=standalone BACKEND_URL=$BACKEND_URL
RUN mkdir -p public && pnpm build

FROM node:20-bookworm-slim
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app app
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static
COPY --from=build --chown=app:app /app/public ./public
USER app
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:3000/login').then(r=>process.exit(r.status<500?0:1)).catch(()=>process.exit(1))"
CMD ["node", "server.js"]
