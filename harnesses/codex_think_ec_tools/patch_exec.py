"""Timeout contract for the exec path, applied after patch_codex_think_ec.py (v5). Every anchor matches exactly once.

Covers: timeout validation + default + clamp to the remaining agent deadline, routing through cg_run, visible
stderr, the structured {timed_out,cancelled,...,cleanup} block, /output/toolcalls.jsonl, and the infra_error.json
+ exit 98 path when cleanup cannot be confirmed. Settings are read from env (CG_TOOL_TIMEOUT_DEFAULT_MS,
CG_TOOL_TIMEOUT_MAX_MS, CG_SUBMIT_TIMEOUT_MS, CG_DEADLINE_EPOCH), validated once at import time.

usage: W2_patch_exec.py <codex-cli/src dir>
"""
import sys
from pathlib import Path

root = Path(sys.argv[1])
EDITS: list[tuple[str, str, str]] = []


def add(relative, old, new):
    EDITS.append((relative, old, new))


RAW_EXEC = "utils/agent/sandbox/raw-exec.ts"
EXEC = "utils/agent/exec.ts"
HANDLE = "utils/agent/handle-exec-command.ts"
AGENT_LOOP = "utils/agent/agent-loop.ts"

# ---------------------------------------------------------------------- raw-exec.ts: spawn through cg_run
add(
    RAW_EXEC,
    'import type { ExecResult } from "./interface";',
    '''import type { ExecResult } from "./interface";
import { cgRunExec } from "./cg-run-exec.js";''',
)
add(
    RAW_EXEC,
    '''export function exec(
  command: Array<string>,
  options: SpawnOptions,
  abortSignal?: AbortSignal,
): Promise<ExecResult> {
  // Adapt command for the current platform (e.g., convert 'ls' to 'dir' on Windows)
  const adaptedCommand = adaptCommandForPlatform(command);''',
    '''export function exec(
  command: Array<string>,
  options: SpawnOptions,
  abortSignal?: AbortSignal,
): Promise<ExecResult> {
  return cgRunExec(command, options, abortSignal, rawExecUnbounded);
}

/** The pre-cg_run implementation. Only cgRunExec (above) may call this: every other path must be bounded. */
function rawExecUnbounded(
  command: Array<string>,
  options: SpawnOptions,
  abortSignal?: AbortSignal,
): Promise<ExecResult> {
  // Adapt command for the current platform (e.g., convert 'ls' to 'dir' on Windows)
  const adaptedCommand = adaptCommandForPlatform(command);''',
)

# New file: cg-run-exec.ts. Builds the cg_run command line, runs it via the existing (now-unbounded) raw exec,
# reads the result file, folds cleanup/timeout/cancellation into the stdout/stderr the rest of codex already expects.
CG_RUN_EXEC_TS = r'''import type { ExecResult } from "./interface";
import { adaptCommandForPlatform } from "../platform-commands.js";
import { log } from "../../logger/log.js";
import { randomUUID } from "crypto";
import * as fsSync from "fs";
import * as os from "os";
import * as path from "path";

export type CgCleanup = { descendants_found: number; killed: number; remaining: number; confirmed: boolean };
export type CgRunResult = ExecResult & {
  cgTimedOut: boolean;
  cgCancelled: boolean;
  cgDeadlineMs: number;
  cgDurationMs: number;
  cgSignal: string | null;
  cgCleanup: CgCleanup | null;
};

const CG_TOOL_TIMEOUT_DEFAULT_MS = cgEnvInt("CG_TOOL_TIMEOUT_DEFAULT_MS", 60_000);
const CG_TOOL_TIMEOUT_MAX_MS = cgEnvInt("CG_TOOL_TIMEOUT_MAX_MS", 300_000);
const CG_SUBMIT_TIMEOUT_MS = cgEnvInt("CG_SUBMIT_TIMEOUT_MS", 240_000);
const CG_DEADLINE_EPOCH = process.env["CG_DEADLINE_EPOCH"] ? Number(process.env["CG_DEADLINE_EPOCH"]) : null;

function cgEnvInt(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw === "") {
    return fallback;
  }
  const value = Number(raw);
  if (!Number.isFinite(value) || !Number.isInteger(value) || value <= 0) {
    throw new Error(`${name}=${raw} is not a positive whole number of milliseconds`);
  }
  return value;
}

if (!(CG_TOOL_TIMEOUT_DEFAULT_MS <= CG_TOOL_TIMEOUT_MAX_MS)) {
  throw new Error(
    `CG_TOOL_TIMEOUT_DEFAULT_MS (${CG_TOOL_TIMEOUT_DEFAULT_MS}) must be <= CG_TOOL_TIMEOUT_MAX_MS (${CG_TOOL_TIMEOUT_MAX_MS})`,
  );
}
if (CG_DEADLINE_EPOCH !== null && !Number.isFinite(CG_DEADLINE_EPOCH)) {
  throw new Error(`CG_DEADLINE_EPOCH=${process.env["CG_DEADLINE_EPOCH"]} is not a number`);
}

/** True for a command whose text invokes submit.sh (the submission class gets the longer default). */
function cgIsSubmission(command: ReadonlyArray<string>): boolean {
  return command.some((part) => /\bsubmit\.sh\b/.test(part));
}

const CG_TOOL_TIMEOUT_MIN_MS = 1000;

/**
 * Validate the model-supplied timeout (ms) against the harness settings. A missing, null or undefined timeout gets
 * the class default. An explicit value must be a whole number of milliseconds between 1000 and the configured
 * maximum; anything else (a string, NaN, zero, negative, fractional, too small, too large) is REJECTED: the command
 * does not run and the message names the valid range. A value is never turned into "no limit".
 */
export function cgResolveTimeout(
  requested: unknown,
  command: ReadonlyArray<string>,
): { ms: number; note: string | null; rejected: string | null } {
  const ceiling = CG_TOOL_TIMEOUT_MAX_MS;
  const fallback = cgIsSubmission(command) ? CG_SUBMIT_TIMEOUT_MS : CG_TOOL_TIMEOUT_DEFAULT_MS;
  if (requested === undefined || requested === null) {
    return { ms: fallback, note: null, rejected: null };
  }
  const valid = typeof requested === "number" && Number.isFinite(requested) && Number.isInteger(requested);
  if (!valid || (requested as number) < CG_TOOL_TIMEOUT_MIN_MS || (requested as number) > ceiling) {
    return {
      ms: 0,
      note: null,
      rejected:
        `the timeout argument (${JSON.stringify(requested)}) was not accepted, so the command was NOT run. ` +
        `timeout is in milliseconds and must be a whole number from ${CG_TOOL_TIMEOUT_MIN_MS} to ${ceiling} ` +
        `(the default is ${fallback} ms when you leave it out).`,
    };
  }
  return { ms: requested as number, note: null, rejected: null };
}

/** Clamp to what is actually left of the agent's own deadline, so no tool call can outlive the evaluation budget. */
function cgClampToAgentDeadline(ms: number): { ms: number; note: string | null } {
  if (CG_DEADLINE_EPOCH === null) {
    return { ms, note: null };
  }
  const remainingMs = Math.round(CG_DEADLINE_EPOCH * 1000 - Date.now()) - 5000;
  if (remainingMs <= 0) {
    return { ms: 0, note: "no time remains before the agent's own deadline" };
  }
  if (remainingMs < ms) {
    return { ms: remainingMs, note: `clamped from ${ms} ms to ${remainingMs} ms, the time left before the agent's own deadline` };
  }
  return { ms, note: null };
}

function cgAppendLog(record: Record<string, unknown>): void {
  try {
    fsSync.appendFileSync("/output/toolcalls.jsonl", JSON.stringify({ time: new Date().toISOString(), ...record }) + "\n");
  } catch {
    /* best effort */
  }
}

function cgFailInfra(kind: string, detail: Record<string, unknown>): never {
  try {
    fsSync.writeFileSync("/output/infra_error.json", JSON.stringify({ kind, ...detail }, null, 2) + "\n");
  } catch {
    /* best effort: the process exit below is what actually matters */
  }
  log(`cg_run: ${kind}: ${JSON.stringify(detail).slice(0, 500)}`);
  process.exit(98);
}

/**
 * Run `command` under cg_run with a deadline, never directly. `unbounded` is raw-exec's own former
 * implementation (now private); this is the ONLY caller allowed to invoke it, and only on the cg_run argv itself.
 */
export async function cgRunExec(
  command: Array<string>,
  options: import("child_process").SpawnOptions,
  abortSignal: AbortSignal | undefined,
  unbounded: (command: Array<string>, options: import("child_process").SpawnOptions, abortSignal?: AbortSignal) => Promise<ExecResult>,
): Promise<CgRunResult> {
  const adapted = adaptCommandForPlatform(command);
  const requestedRaw = (options as { cgRequestedTimeoutMs?: unknown }).cgRequestedTimeoutMs;
  const resolved = cgResolveTimeout(requestedRaw, adapted);
  if (resolved.rejected !== null) {
    const rejectedResult: CgRunResult = {
      stdout: "", stderr: `Rejected: ${resolved.rejected}`, exitCode: 1,
      cgTimedOut: false, cgCancelled: false, cgDeadlineMs: 0, cgDurationMs: 0, cgSignal: null, cgCleanup: null,
    };
    cgAppendLog({
      command: adapted.join(" ").slice(0, 2000), deadline_ms: 0, timed_out: false, cancelled: false,
      blocked: true, rejected: true, notes: [resolved.rejected],
    });
    return rejectedResult;
  }
  const clamped = cgClampToAgentDeadline(resolved.ms);
  const notes = [resolved.note, clamped.note].filter((n): n is string => n != null);
  if (clamped.ms <= 0) {
    const execResult: CgRunResult = {
      stdout: "", stderr: `Blocked: ${clamped.note}`, exitCode: 1,
      cgTimedOut: false, cgCancelled: false, cgDeadlineMs: 0, cgDurationMs: 0, cgSignal: null, cgCleanup: null,
    };
    cgAppendLog({ command: adapted.join(" ").slice(0, 2000), deadline_ms: clamped.ms, timed_out: false, cancelled: false, blocked: true, notes });
    return execResult;
  }

  const resultFile = path.join(os.tmpdir(), `cg_run_result_${randomUUID()}.json`);
  const cgArgv = ["cg_run", "--deadline-ms", String(clamped.ms), "--result-file", resultFile, "--", ...adapted];
  const started = Date.now();
  const raw = await unbounded(cgArgv, options, abortSignal);
  const durationMs = Date.now() - started;

  let parsed: { timed_out: boolean; cancelled: boolean; cancel_reason: string | null; deadline_ms: number; exit_code: number | null; signal: string | null; cleanup: CgCleanup; } | null = null;
  try {
    parsed = JSON.parse(fsSync.readFileSync(resultFile, "utf8"));
  } catch {
    parsed = null;
  } finally {
    try {
      fsSync.unlinkSync(resultFile);
    } catch {
      /* already gone */
    }
  }

  if (parsed === null || !parsed.cleanup.confirmed) {
    cgFailInfra("unconfirmed_cleanup", { command: adapted.join(" ").slice(0, 2000), supervisor_exit_code: raw.exitCode, parsed });
  }

  let stderr = raw.stderr;
  if (parsed.timed_out) {
    stderr += `\n[the command did not finish within ${parsed.deadline_ms} ms and was terminated; every process it started was confirmed killed]`;
  } else if (parsed.cancelled) {
    stderr += `\n[the command was cancelled (${parsed.cancel_reason}); every process it started was confirmed killed]`;
  }
  for (const note of notes) {
    stderr += `\n[${note}]`;
  }

  const execResult: CgRunResult = {
    stdout: raw.stdout, stderr, exitCode: parsed.exit_code ?? raw.exitCode,
    cgTimedOut: parsed.timed_out, cgCancelled: parsed.cancelled, cgDeadlineMs: parsed.deadline_ms,
    cgDurationMs: durationMs, cgSignal: parsed.signal, cgCleanup: parsed.cleanup,
  };
  cgAppendLog({
    command: adapted.join(" ").slice(0, 2000), deadline_ms: parsed.deadline_ms, timed_out: parsed.timed_out,
    cancelled: parsed.cancelled, exit_code: execResult.exitCode, duration_ms: durationMs, cleanup: parsed.cleanup, notes,
  });
  return execResult;
}
'''
assert sys.argv[0]  # keep type checkers quiet about the unused f-string style below


# ---------------------------------------------------------------------- exec.ts: pass the requested timeout + submit class through
add(
    EXEC,
    'const DEFAULT_TIMEOUT_MS = 10_000; // 10 seconds\n',
    '// DEFAULT_TIMEOUT_MS removed: cg-run-exec.ts resolves the timeout (class default, max, agent-deadline clamp).\n',
)
add(
    EXEC,
    '''  const opts: SpawnOptions = {
    timeout: timeoutInMillis || DEFAULT_TIMEOUT_MS,
    ...(requiresShell(cmd) ? { shell: true } : {}),
    ...(workdir ? { cwd: workdir } : {}),
  };''',
    '''  const opts: SpawnOptions & { cgRequestedTimeoutMs?: unknown } = {
    // node's own child_process timeout is NOT the enforcement path (cg_run is); kept only as a last-resort backstop
    // at the ceiling so a spawn() call can never itself block forever if cg_run were somehow bypassed.
    timeout: 300_000,
    cgRequestedTimeoutMs: timeoutInMillis,
    ...(requiresShell(cmd) ? { shell: true } : {}),
    ...(workdir ? { cwd: workdir } : {}),
  };''',
)

# ---------------------------------------------------------------------- macos-seatbelt.ts: carries the same options through to rawExec; no change needed, it already calls exec() from raw-exec.ts.

# ---------------------------------------------------------------------- handle-exec-command.ts: surface the structured fields
add(
    HANDLE,
    '''function convertSummaryToResult(
  summary: ExecCommandSummary,
): HandleExecCommandResult {
  const { stdout, stderr, exitCode, durationMs } = summary;
  return {
    outputText: stdout || stderr,
    metadata: {
      exit_code: exitCode,
      duration_seconds: Math.round(durationMs / 100) / 10,
    },
  };
}

type ExecCommandSummary = {
  stdout: string;
  stderr: string;
  exitCode: number;
  durationMs: number;
};''',
    '''function convertSummaryToResult(
  summary: ExecCommandSummary,
): HandleExecCommandResult {
  const { stdout, stderr, exitCode, durationMs, timedOut, cancelled, deadlineMs, cleanup } = summary;
  const outputText = [stdout, stderr].filter((part) => part).join(stdout && stderr ? "\\n" : "");
  return {
    outputText: outputText || (exitCode === 0 ? "" : `[exit code ${exitCode}]`),
    metadata: {
      exit_code: exitCode,
      duration_seconds: Math.round(durationMs / 100) / 10,
      timed_out: timedOut,
      cancelled: cancelled,
      deadline_ms: deadlineMs,
      cleanup: cleanup,
    },
  };
}

type ExecCommandSummary = {
  stdout: string;
  stderr: string;
  exitCode: number;
  durationMs: number;
  timedOut: boolean;
  cancelled: boolean;
  deadlineMs: number;
  cleanup: unknown;
};''',
)
add(
    HANDLE,
    '''  const duration = Date.now() - start;
  const { stdout, stderr, exitCode } = execResult;

  if (isLoggingEnabled()) {
    log(
      `EXEC exit=${exitCode} time=${duration}ms:\\n\\tSTDOUT: ${stdout}\\n\\tSTDERR: ${stderr}`,
    );
  }

  return {
    stdout,
    stderr,
    exitCode,
    durationMs: duration,
  };
}''',
    '''  const duration = Date.now() - start;
  const { stdout, stderr, exitCode } = execResult;
  const cg = execResult as Partial<{ cgTimedOut: boolean; cgCancelled: boolean; cgDeadlineMs: number; cgCleanup: unknown }>;

  if (isLoggingEnabled()) {
    log(
      `EXEC exit=${exitCode} time=${duration}ms timed_out=${cg.cgTimedOut ?? false}:\\n\\tSTDOUT: ${stdout}\\n\\tSTDERR: ${stderr}`,
    );
  }

  return {
    stdout,
    stderr,
    exitCode,
    durationMs: duration,
    timedOut: cg.cgTimedOut ?? false,
    cancelled: cg.cgCancelled ?? false,
    deadlineMs: cg.cgDeadlineMs ?? 0,
    cleanup: cg.cgCleanup ?? null,
  };
}''',
)
# apply_patch (no subprocess at all; execApplyPatch stays synchronous fs I/O) needs the same shape on its branch.
add(
    HANDLE,
    '''  const execResult =
    applyPatchCommand != null
      ? execApplyPatch(applyPatchCommand.patch, workdir)
      : await exec(''',
    '''  const execResult: ExecResult & { cgTimedOut?: boolean; cgCancelled?: boolean; cgDeadlineMs?: number; cgCleanup?: unknown } =
    applyPatchCommand != null
      ? execApplyPatch(applyPatchCommand.patch, workdir)
      : await exec(''',
)

# ---------------------------------------------------------------------- agent-loop.ts: the shell tool description
add(
    AGENT_LOOP,
    '''const shellTool: FunctionTool = {
  type: "function",
  name: "shell",
  description: "Runs a shell command, and returns its output.",
  strict: false,
  parameters: {
    type: "object",
    properties: {
      command: { type: "array", items: { type: "string" } },
      workdir: {
        type: "string",
        description: "The working directory for the command.",
      },
      timeout: {
        type: "number",
        description:
          "The maximum time to wait for the command to complete in milliseconds.",
      },
    },
    required: ["command"],
    additionalProperties: false,
  },
};''',
    '''const shellTool: FunctionTool = {
  type: "function",
  name: "shell",
  description:
    "Runs a shell command and returns its output. Every call has a deadline: 60 s by default, or set timeout " +
    "(milliseconds) up to 300 s for a command you expect to take longer. When the call ends for any reason " +
    "(it finishes, it hits its deadline, or it is cancelled), every process it started, including anything " +
    "backgrounded with &, nohup, setsid or disown, is killed before the call returns. Nothing you start in a " +
    "shell call keeps running after that call ends; start a loop again in a later call if you need to keep polling.",
  strict: false,
  parameters: {
    type: "object",
    properties: {
      command: { type: "array", items: { type: "string" } },
      workdir: {
        type: "string",
        description: "The working directory for the command.",
      },
      timeout: {
        type: "number",
        description:
          "The deadline for this command, in milliseconds. Default 60000 (60 s) when you leave it out. " +
          "A whole number from 1000 to 300000 (300 s); any other value is rejected and the command does not run.",
      },
    },
    required: ["command"],
    additionalProperties: false,
  },
};''',
)

for relative, old, new in EDITS:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"{relative}: anchor not found exactly once ({text.count(old)}): {old[:70]!r}")
    path.write_text(text.replace(old, new), encoding="utf-8")
    print(f"patched {relative}")

new_file = root / "utils/agent/sandbox/cg-run-exec.ts"
new_file.write_text(CG_RUN_EXEC_TS, encoding="utf-8")
print(f"wrote {new_file}")
