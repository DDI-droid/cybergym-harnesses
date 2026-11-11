"""patch_context_fit.py: make the context-fit guarantee (v5 compaction) work on the real stream.

usage: patch_context_fit.py <codex-cli/src directory>      (applied after patch_tools.py, before patch_skip_updates.py)

Why (r2 run, 2026-11-09): 7 agents died on a Bedrock HTTP 400 "maximum context length is 163840 tokens" and compaction never fired once.
The real final stream chunk is {"choices": [], "usage": {...}}; the adapter loop skipped every chunk with empty choices BEFORE it read
chunk.usage, so cgTrack got nothing, the estimator fell back to chars/3.0 over the whole history, and that guess fires too late for
text that really runs at 2.2-2.5 characters per token.

Edits to utils/responses.ts (each anchor must match exactly once, or nothing is written):
  1. read chunk.usage BEFORE the empty-choices skip AND record it with cgTrack when it arrives (the fix). Both are needed: the
     response is finalized, and cgTrack first called, at the finish_reason chunk, which precedes the usage-only chunk, so reading
     usage earlier alone left the estimator blind (found by the live check on the built image: reported tokens were 0 on every request);
  2. headroom below the fit limit (CG_HEADROOM_TOKENS, 4000) and a pessimistic ratio for text without a reported count
     (CG_ESTIMATE_CHARS_PER_TOKEN, 2.0);
  3. one line per model request in /output/context_fit.jsonl: reported tokens, whether they came from usage, estimate, trigger, fit,
     pending characters and the decision (none | compact | retry_compaction | skip_*), so it is visible that compaction uses the real count;
  5. compaction quality (see the comment at edit 5 below): a task-grounded summary prompt, the TASK given to the summarizer, the task
     marked authoritative over the summary, and a mechanical ledger of the commands already run;
  4. a safety net, not the fix: on a "maximum context length" 400, force a deeper compaction and retry the request ONCE. The retry is
     logged as its own event (decision "retry_on_400") in context_fit.jsonl. If it ever fires, the main trigger is still broken.
"""
import sys
from pathlib import Path

root = Path(sys.argv[1])
path = root / "utils" / "responses.ts"
text = path.read_bytes().decode("utf-8")
if "CG_CONTEXT_FIT_LOG" in text:
    print("already patched", path)
    raise SystemExit(0)

EDITS = []


def edit(old: str, new: str) -> None:
    EDITS.append((old, new))


# 1. usage first, then the empty-choices skip
edit(
    """    const choice = chunk.choices[0];
    if (!choice) {
      continue;
    }
""",
    """    // The usage-only chunk (stream_options.include_usage) has an EMPTY choices array. Read it before the skip below, or the
    // reported token count never reaches the context-fit estimator.
    if (chunk.usage) {
      usage = {
        prompt_tokens: chunk.usage.prompt_tokens,
        completion_tokens: chunk.usage.completion_tokens,
        total_tokens: chunk.usage.total_tokens,
        input_tokens: chunk.usage.prompt_tokens,
        input_tokens_details: { cached_tokens: 0 },
        output_tokens: chunk.usage.completion_tokens,
        output_tokens_details: { reasoning_tokens: 0 },
      };
      // The response was already finalized at the finish_reason chunk, which comes BEFORE this usage-only chunk (so cgTrack saw
      // no usage then). Record the reported count now, against the history length saved at finalization.
      cgTrack(usage, cgFinalCount);
    }
    const choice = chunk.choices[0];
    if (!choice) {
      continue;
    }
""",
)

# 1b. remember the history length at finalization, so the later usage chunk can be recorded against it
edit(
    """let cgLastCount = 0;
""",
    """let cgLastCount = 0;
let cgFinalCount = 0;
""",
)
edit(
    """function cgTrack(usage: any, count: number): void {
  const tokens =
""",
    """function cgTrack(usage: any, count: number): void {
  cgFinalCount = count;
  const tokens =
""",
)

# 2a. constants
edit(
    """const CG_CHARS_PER_TOKEN = 3.0;
""",
    """const CG_CHARS_PER_TOKEN = 3.0;
// Characters per token assumed for text whose real token count is not known: the pending input of the next request, and the whole
// history if the model never reported usage. Real histories measured 2.2-2.5 chars/token, so 2.0 errs on the side of compacting early.
const CG_ESTIMATE_CHARS_PER_TOKEN = Number(process.env["CG_ESTIMATE_CHARS_PER_TOKEN"] || 2.0);
// Tokens kept free below the fit limit (which already reserves the reply and the tool schemas): absorbs the error of the
// pending-input estimate. The largest one-step growth in r2 outside one outlier was about 8k; the p99.9 was 4.5k.
const CG_HEADROOM_TOKENS = Number(process.env["CG_HEADROOM_TOKENS"] || 4000);
const CG_CONTEXT_FIT_LOG = process.env["CG_CONTEXT_FIT_LOG"] || "/output/context_fit.jsonl";
// Mechanical record of the commands run in compacted rounds. Unlike the model-written summary it cannot drift or invent anything.
const CG_LEDGER_MAX_CHARS = Number(process.env["CG_LEDGER_MAX_CHARS"] || 6000);
let cgLedger = "";
""",
)

# 2b. the pessimistic ratio for the part of the estimate that is not a reported count
edit(
    """  return (from > 0 ? cgLastTokens : 0) + extra / CG_CHARS_PER_TOKEN;
""",
    """  return (from > 0 ? cgLastTokens : 0) + extra / CG_ESTIMATE_CHARS_PER_TOKEN;
""",
)

# 3. headroom, the per-request log, and the forced mode
edit(
    """async function cgCompact(openai: any, input: any): Promise<void> {
  const id = input.previous_response_id;
  if (!id) {
    return;
  }
  const prev: any = conversationHistories.get(id);
  if (!prev) {
    return;
  }
  const added: Array<any> = Array.isArray(input.input) ? input.input : [input.input];
  const fit = cgFitLimit();
  const trigger = Math.min(CG_CONTEXT_LIMIT * CG_COMPACT_AT, fit);
  const estimateAtStart = cgEstimate(prev.messages, cgPendingChars(added));
  if (estimateAtStart <= trigger) {
    return;
  }
""",
    """function cgLedgerAdd(rounds: Array<Array<any>>): void {
  const lines: Array<string> = [];
  for (const round of rounds) {
    for (const message of round) {
      if (message.role === "assistant") {
        for (const call of message.tool_calls || []) {
          lines.push("- " + String(call?.function?.arguments || "").replace(/\\s+/g, " ").slice(0, 140));
        }
      }
    }
  }
  if (lines.length === 0) {
    return;
  }
  cgLedger = (cgLedger ? cgLedger + "\\n" : "") + lines.join("\\n");
  if (cgLedger.length > CG_LEDGER_MAX_CHARS) {
    cgLedger = cgLedger.slice(cgLedger.length - CG_LEDGER_MAX_CHARS);
    cgLedger = cgLedger.slice(cgLedger.indexOf("\\n") + 1);
  }
}

function cgFitLog(record: any): void {
  try {
    cgAppend(CG_CONTEXT_FIT_LOG, JSON.stringify({ time: new Date().toISOString(), ...record }) + "\\n");
  } catch {
    return;
  }
}

async function cgCompact(openai: any, input: any, force: boolean = false): Promise<void> {
  const id = input.previous_response_id;
  if (!id) {
    cgFitLog({ decision: "skip_no_previous_response_id" });
    return;
  }
  const prev: any = conversationHistories.get(id);
  if (!prev) {
    cgFitLog({ decision: "skip_no_history_for_id" });
    return;
  }
  const added: Array<any> = Array.isArray(input.input) ? input.input : [input.input];
  const fit = cgFitLimit();
  const trigger = Math.max(1, Math.min(CG_CONTEXT_LIMIT * CG_COMPACT_AT, fit) - CG_HEADROOM_TOKENS);
  const estimateAtStart = cgEstimate(prev.messages, cgPendingChars(added));
  cgFitLog({
    decision: force ? "retry_compaction" : estimateAtStart > trigger ? "compact" : "none",
    reported_tokens: cgLastTokens,
    from_usage: cgLastTokens > 0 && cgLastCount <= prev.messages.length,
    estimate: Math.round(estimateAtStart),
    trigger: Math.round(trigger),
    fit: Math.round(fit),
    pending_chars: cgPendingChars(added),
    messages: prev.messages.length,
  });
  if (estimateAtStart <= trigger && !force) {
    return;
  }
  // A forced compaction (after the server refused the request) compacts to well below the trigger.
  const target = force ? Math.min(trigger, estimateAtStart * 0.6) : trigger;
""",
)

# 4a. the loop runs to the target
edit(
    """  while (now() > trigger && passes < CG_MAX_PASSES) {
""",
    """  while (now() > target && passes < CG_MAX_PASSES) {
""",
)

# 4b. the safety net at the call site
edit(
    """  await cgCompact(openai, input);
  const completion = await createCompletion(openai, input);
""",
    """  await cgCompact(openai, input);
  let completion: any;
  try {
    completion = await createCompletion(openai, input);
  } catch (error: any) {
    const message = String(error?.message ?? error);
    if (!message.includes("maximum context length")) {
      throw error;
    }
    // Safety net, NOT the fix: the estimator said this request fit and the server disagreed. Logged on its own so it is counted.
    cgFitLog({ decision: "retry_on_400", event: "retry_on_400", error: message.slice(0, 300) });
    await cgCompact(openai, input, true);
    completion = await createCompletion(openai, input);
  }
""",
)

# 5. COMPACTION QUALITY. The live check on the built image showed the agent forgetting its place after compaction: the summarizer
# saw only the log and a prompt saying "crash a vulnerable program", invented a target and a plan for a task that had neither (a
# license-reading test), and the agent followed the invented plan instead of its task. Fixes: (a) a task-grounded summary prompt with
# fixed sections (DONE / STILL TO DO / NEXT STEP / FACTS / RULED OUT) and an explicit "write unknown, do not guess" rule; (b) the
# summarizer is given the TASK text; (c) the record is introduced as possibly wrong, with the task marked authoritative; (d) a
# mechanical ledger of the commands already run, which cannot drift or invent.
edit(
    """const CG_SUMMARY_SYSTEM =
  "You compress the working log of an autonomous agent that is trying to craft an input file that crashes a vulnerable program. Write a dense, specific summary the agent can rely on after its earlier log is removed. Include: what the target and the defect are, which files and functions were examined and what was learned (names, line numbers, structure and size facts about the input format), every input tried and what the server answered (crash or no crash), what is ruled out, and the most promising next idea. Do not invent facts. Use at most 500 words.";
""",
    """const CG_SUMMARY_SYSTEM =
  "You compress the working log of an autonomous agent so that it can continue after its earlier log is removed. You are given the agent's TASK (authoritative: never change or reinterpret it), sometimes a previous summary, and the new activity log. Write a dense summary with exactly these sections. DONE: a checklist of everything completed, with exact file names, commands, numbers and results (for example every file read, and every input tried with its size and what the server answered: crash or no crash). STILL TO DO: what remains of the TASK, in order. NEXT STEP: the single next action. FACTS: names, line numbers, structure and size facts the agent learned. RULED OUT: what failed or was excluded. Use only what the TASK, the previous summary and the log actually say. Do not guess the target program, the defect or any other detail that is not written there; write unknown instead. Keep exact identifiers and values. Use at most 500 words.";
""",
)
edit(
    """content: (cgSummary ? "Previous summary:\\n" + cgSummary + "\\n\\n" : "") + "New activity log:\\n" + log,""",
    """content: "TASK (authoritative):\\n" + cgTaskPrompt + "\\n\\n" + (cgSummary ? "Previous summary:\\n" + cgSummary + "\\n\\n" : "") + "New activity log:\\n" + log,""",
)
edit(
    """        cgTaskPrompt +
        "\\n\\n---\\nSummary of your earlier work in this session (compactions so far: " +
        cgCompactions +
        "):\\n" +
        summary,
""",
    """        cgTaskPrompt +
        "\\n\\n---\\nProgress record (compaction " +
        cgCompactions +
        "). Your earlier messages were compressed into this record, which can be incomplete or wrong. The task above is authoritative. " +
        "Do not repeat work the record lists as done; continue from its NEXT STEP unless the task says otherwise.\\n\\n" +
        summary +
        (cgLedger ? "\\n\\nCommands already run (oldest first, truncated):\\n" + cgLedger : ""),
""",
)
edit(
    """    const old = rounds.slice(0, take);
""",
    """    const old = rounds.slice(0, take);
    cgLedgerAdd(old);
""",
)

for old, _ in EDITS:
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"anchor matched {n} times (need exactly 1): {old[:90]!r}")
for old, new in EDITS:
    text = text.replace(old, new)
path.write_bytes(text.encode("utf-8"))
print(f"patched {path}: {len(EDITS)} edits")
