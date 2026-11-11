FROM node:20 AS build
WORKDIR /src
COPY --from=upstream . /src
COPY patch_codex.py /patch_codex.py
RUN python3 /patch_codex.py /src
RUN corepack enable \
 && cd codex-cli \
 && pnpm install \
 && pnpm run build \
 && pnpm pack --pack-destination ./dist \
 && mv ./dist/openai-codex-*.tgz ./dist/codex.tgz

FROM node:20-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash curl ca-certificates git ripgrep jq procps file xxd python3 less unzip \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/codex-cli/dist/codex.tgz /tmp/codex.tgz
RUN npm install -g /tmp/codex.tgz && rm /tmp/codex.tgz && npm cache clean --force
ENV CODEX_UNSAFE_ALLOW_NO_SANDBOX=1 \
    HOME=/tmp/home
RUN mkdir -p /tmp/home && chmod 777 /tmp/home
WORKDIR /workspace
