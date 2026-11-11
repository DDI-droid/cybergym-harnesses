# cybergym-harnesses

Harness definitions for [CyberGym](https://github.com/sunblaze-ucb/cybergym) agents: a Dockerfile per harness, plus the entrypoint, process supervisor, and patches that get bind-mounted into the container at runtime rather than baked into the image. The bind-mounted code is hash-checked per label, so a label can never silently drift to different code behind the same name.

Most of these build on Codex, patched line-by-line against its own TypeScript source — extended context handling, a process-tree supervisor that kills the whole descendant tree on timeout instead of leaving zombies behind, a typed submission client with its own concurrency gate, and a few reasoning-effort variants to compare against. Each patch asserts its anchor text matches exactly once before touching anything; if upstream moves under it, the build fails loudly instead of patching the wrong spot quietly.

Why these exist, and what actually changed on the benchmark because of them: [prescera interview report](https://ddi-droid.github.io/blog/2026/prescera-interview-report/).
