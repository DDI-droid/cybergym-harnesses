import sys
from pathlib import Path

PARSERS = "codex-cli/src/utils/parsers.ts"
RAW_EXEC = "codex-cli/src/utils/agent/sandbox/raw-exec.ts"
ADAPTER = "codex-cli/src/utils/responses.ts"
AGENT_LOOP = "codex-cli/src/utils/agent/agent-loop.ts"

THOUGHTS = r"""import { appendFileSync as cgAppend } from "fs";

function cgSaveText(text: string): void {
  if (!text) {
    return;
  }
  try {
    cgAppend("/output/thoughts.jsonl", JSON.stringify({ time: new Date().toISOString(), text }) + "\n");
  } catch {
    return;
  }
}

// Utility function to generate unique IDs"""

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
    (ADAPTER, "// Utility function to generate unique IDs", THOUGHTS),
    (
        ADAPTER,
        "  return openai.chat.completions.create(chatInput);",
        """  Object.assign(chatInput as object, { reasoning_effort: "high" });
  return openai.chat.completions.create(chatInput);""",
    ),
    (
        ADAPTER,
        """    if (textContent) {
      assistantMessage.content = textContent;
    }""",
        r"""    const cgVisible = textContent
      .replace(/<reasoning>[\s\S]*?<\/reasoning>/g, "")
      .replace(/<｜DSML｜[\s\S]*$/, "")
      .replace(/<\/think>/g, "")
      .trim();
    if (cgVisible) {
      assistantMessage.content = cgVisible;
    } else if (textContent && toolCalls.size === 0) {
      assistantMessage.content = "(no visible reply)";
    }""",
    ),
    (
        AGENT_LOOP,
        """      emitItem(item as ResponseItem);
    }
    return turnInput;
  }""",
        r"""      emitItem(item as ResponseItem);
    }
    if (turnInput.length === 0 && JSON.stringify(output).includes("DSML")) {
      turnInput.push({
        type: "message",
        role: "user",
        content: [
          {
            type: "input_text",
            text: "Your last reply wrote a tool call as plain text, so nothing was executed. Call the shell tool properly to continue.",
          },
        ],
      } as ResponseInputItem);
    }
    return turnInput;
  }""",
    ),
    (
        ADAPTER,
        """    yield { type: "response.completed", response: finalResponse };""",
        """    cgSaveText(textContent);
    yield { type: "response.completed", response: finalResponse };""",
    ),
]

root = Path(sys.argv[1])
for relative, old, new in EDITS:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"{relative}: upstream code changed, patch anchor not found exactly once: {old[:70]!r}")
    path.write_text(text.replace(old, new), encoding="utf-8")
    print(f"patched {relative}")
