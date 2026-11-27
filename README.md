# cybergym-harnesses

Harness definitions for [CyberGym](https://github.com/sunblaze-ucb/cybergym) agents: a `harness.toml` and a Dockerfile per harness, plus the entrypoint and scripts that get bind-mounted read-only into the container at `/harness` at runtime. The Codex patches and the process supervisor are applied and installed when `build.sh` builds the image. The runner hash-checks each harness folder and records its image id per label, so a label can never silently drift to different code behind the same name.

Each `harness.toml` names the models the harness uses, for example `models = ["deepseek.v3.2"]`. The runner passes them to the container as `CYBERGYM_MODELS`, a JSON list, and the harness's code picks from it (all harnesses here use one model, `CYBERGYM_MODELS[0]`). Where a model is served and its credential stay with the eval's run config and its LLM shim, and never reach the container; the shim refuses any model a harness did not list. Changing a harness's model is a change to the harness, so it gets a new label.

Most of these build on Codex, patched line-by-line against its own TypeScript source — extended context handling, a process-tree supervisor that kills the whole descendant tree on timeout instead of leaving zombies behind, a typed submission client with its own concurrency gate, and a few reasoning-effort variants to compare against. Each patch asserts its anchor text matches exactly once before touching anything; if upstream moves under it, the build fails loudly instead of patching the wrong spot quietly.

Why these exist, and what actually changed on the benchmark because of them: [prescera interview report](https://ddi-droid.github.io/blog/2026/prescera-interview-report/).
