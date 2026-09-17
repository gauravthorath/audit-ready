# The React front end, self-hosted (CR-4, CR-8).
#
# Three stages: install, build, run. The runtime carries only Next's
# standalone output — the server, its traced node_modules, and the static
# assets — so the image is the app and not the toolchain.
#
# One thing to know about this image: NEXT_PUBLIC_API_URL is inlined into the
# browser bundle at BUILD time. It is where the user's *browser* reaches the
# API, not where this container does — so the default is the API's published
# host port, and a deployment on another host rebuilds with its own value:
#
#   docker compose build --build-arg NEXT_PUBLIC_API_URL=https://audit.example/api web

# ---------------------------------------------------------------------------
FROM node:20-alpine AS deps
RUN corepack enable && corepack prepare pnpm@9.15.9 --activate
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile

# ---------------------------------------------------------------------------
FROM node:20-alpine AS build
RUN corepack enable && corepack prepare pnpm@9.15.9 --activate
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_TELEMETRY_DISABLED=1
RUN pnpm build

# ---------------------------------------------------------------------------
FROM node:20-alpine AS runtime
RUN addgroup -S web && adduser -S -G web -u 10001 web
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Standalone output: server.js plus exactly the node_modules it traced.
COPY --from=build --chown=web:web /app/.next/standalone ./
COPY --from=build --chown=web:web /app/.next/static ./.next/static
COPY --from=build --chown=web:web /app/public ./public

USER web
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1

CMD ["node", "server.js"]
