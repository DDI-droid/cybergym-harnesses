FROM node:20 AS build
WORKDIR /src
COPY --from=upstream . /src
COPY patch_codex_think_ec.py /patch_codex_think_ec.py
RUN python3 /patch_codex_think_ec.py /src
COPY patch_tools.py /patch_tools.py
RUN python3 /patch_tools.py /src/codex-cli/src
COPY patch_context_fit.py /patch_context_fit.py
RUN python3 /patch_context_fit.py /src/codex-cli/src
COPY patch_skip_updates.py /patch_skip_updates.py
RUN python3 /patch_skip_updates.py /src/codex-cli/src
RUN corepack enable \
 && cd codex-cli \
 && pnpm install \
 && pnpm run typecheck \
 && pnpm run build \
 && pnpm pack --pack-destination ./dist \
 && mv ./dist/openai-codex-*.tgz ./dist/codex.tgz

FROM node:20-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash curl ca-certificates git ripgrep jq procps file xxd python3 less unzip \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/codex-cli/dist/codex.tgz /tmp/codex.tgz
RUN npm install -g /tmp/codex.tgz && rm /tmp/codex.tgz && npm cache clean --force
COPY cg_run.py /usr/local/bin/cg_run
RUN chmod 755 /usr/local/bin/cg_run
# No program in the image may run with another user's rights: the agent must not be able to
# become root and take over the file the evaluator locks.
RUN find / -xdev -type f \( -perm -4000 -o -perm -2000 \) -exec chmod a-s {} + 2>/dev/null; true
ENV CODEX_UNSAFE_ALLOW_NO_SANDBOX=1 \
    HOME=/tmp/home
RUN mkdir -p /tmp/home && chmod 777 /tmp/home
WORKDIR /workspace
