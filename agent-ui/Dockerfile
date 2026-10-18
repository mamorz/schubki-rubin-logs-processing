# RUBIN agent UI: React/Fastify front end driving Kiro CLI over ACP.
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build && npm prune --omit=dev

FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS kiro
ARG KIRO_CLI_VERSION=2.32.0
ARG KIRO_CLI_SHA256_X86_64=7cea2c90198edc60ccaee9e0d9c9de26902b3824f54a66aa3639a365f7242fbb
ARG KIRO_CLI_SHA256_AARCH64=f66faaa3fa202e7730979e10ad907c43f83220577609c97fdffbb07fc8ef1848
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "$(uname -m)" in \
      x86_64) arch=x86_64; sum="$KIRO_CLI_SHA256_X86_64" ;; \
      aarch64) arch=aarch64; sum="$KIRO_CLI_SHA256_AARCH64" ;; \
      *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/kirocli.zip "https://desktop-release.q.us-east-1.amazonaws.com/${KIRO_CLI_VERSION}/kirocli-${arch}-linux.zip"; \
    echo "$sum  /tmp/kirocli.zip" | sha256sum -c -; \
    unzip -q /tmp/kirocli.zip -d /tmp; \
    install -m 0755 /tmp/kirocli/bin/kiro-cli /tmp/kirocli/bin/kiro-cli-chat /tmp/kirocli/bin/kiro-cli-term /usr/local/bin/

FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && mkdir -p /workspace/.kiro/agents && chown -R app:app /workspace
COPY --from=kiro /usr/local/bin/kiro-cli* /usr/local/bin/
WORKDIR /opt/agent-ui
COPY --from=build /src/package.json ./
COPY --from=build /src/node_modules ./node_modules
COPY --from=build /src/dist ./dist
COPY fallback-agents ./fallback-agents
COPY docker/entrypoint.sh /usr/local/bin/agent-ui-entrypoint
ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=8080 \
    AGENT_WORKSPACE=/workspace \
    RUBIN_MCP_URL=http://mcp-server:8000/mcp \
    STATIC_DIR=/opt/agent-ui/dist/web
USER app
EXPOSE 8080
ENTRYPOINT ["tini", "--", "agent-ui-entrypoint"]
CMD ["node", "dist/server/server/index.js"]
