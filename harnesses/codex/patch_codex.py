import sys
from pathlib import Path

PARSERS = "codex-cli/src/utils/parsers.ts"
RAW_EXEC = "codex-cli/src/utils/agent/sandbox/raw-exec.ts"

EDITS = [
    (
        PARSERS,
        """    (typeof cmd === "string" ? [cmd] : undefined) ??
    (typeof command === "string" ? [command] : undefined);""",
        """    fromCommandString(cmd) ??
    fromCommandString(command);""",
    ),
    (
        PARSERS,
        "function toStringArray(obj: unknown): Array<string> | undefined {",
        """function fromCommandString(value: unknown): Array<string> | undefined {
  if (typeof value !== "string") {
    return undefined;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    parsed = undefined;
  }
  return toStringArray(parsed) ?? ["bash", "-lc", value];
}

function toStringArray(obj: unknown): Array<string> | undefined {""",
    ),
    (
        RAW_EXEC,
        "  const child: ChildProcess = spawn(prog, adaptedCommand.slice(1), fullOptions);",
        """  let child: ChildProcess;
  try {
    child = spawn(prog, adaptedCommand.slice(1), fullOptions);
  } catch (error) {
    return Promise.resolve({
      stdout: "",
      stderr: `failed to start the command: ${String(error).slice(0, 500)}`,
      exitCode: 127,
    });
  }""",
    ),
]

root = Path(sys.argv[1])
for relative, old, new in EDITS:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"{relative}: upstream code changed, patch anchor not found exactly once")
    path.write_text(text.replace(old, new), encoding="utf-8")
    print(f"patched {relative}")
