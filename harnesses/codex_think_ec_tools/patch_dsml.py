"""codex_think_ec_tools / W1: recover DeepSeek's native DSML tool call instead of discarding it.

Applied to the SAME upstream codex-cli source tree, AFTER patch_codex_think_ec.py (the v5 patch) has already run.
Touches only responses.ts. Does not touch raw-exec.ts, handle-exec-command.ts or the shell tool schema (W2's files).

Evidence (see the report): live-captured, through the real shim, with the real shell tool schema and realistic
multi-turn histories. 37% of samples in realistic in-progress-investigation histories got finish_reason="stop" with
zero structured tool_calls, and in every one of those the model's own content held a COMPLETE, well-formed native
function-call block (reasoning tags interleaved at arbitrary byte offsets, stripped first). Grammar, confirmed
against >15 raw samples:
    <ﹷDSMLﹷfunction_calls>            (ufe77 placeholder; real char is the fullwidth vertical line below)
    <DSML-invoke name="TOOL">
      <DSML-parameter name="PARAM" string="true|false">VALUE</DSML-parameter>  (repeat per parameter)
    </DSML-invoke>                       (repeat per parallel call)
    </DSML-function_calls>
string="false" -> VALUE is JSON (e.g. the command array as a literal JSON array). string="true" -> VALUE is a raw
string; for "command" specifically this is a single shell line, wrapped as ["bash","-lc",VALUE] to match the
existing raw-exec convention. A dangling/partial block (no match, or any parameter whose string="false" value
fails JSON.parse) recovers nothing and falls through unchanged to the existing nudge path. Never touches a turn
where a structured delta.tool_calls already arrived (toolCalls.size > 0): no double execution.
"""
import sys
from pathlib import Path

ADAPTER = "codex-cli/src/utils/responses.ts"

HELPERS = r"""function cgStripReasoning(text: string): string {
  // The real reasoning stripper (cgVisible, above) removes <reasoning>...content...</reasoning> as a unit, which
  // is correct for hiding genuine chain-of-thought from the stored reply. It is wrong here: when DeepSeek streams
  // its native function-call text through the reasoning_content channel, this adapter wraps EVERY chunk of it in
  // its own <reasoning>...</reasoning> pair, chunk by chunk, with no gap between consecutive pairs - the DSML
  // syntax itself ends up split across many wrapped fragments, not hidden inside a real reasoning aside. Removing
  // only the tag delimiters (never the text between them) reassembles the original, unbroken stream byte for
  // byte; confirmed against >20 raw captures, including real sentences reassembling correctly.
  return text.replace(/<\/?reasoning>/g, "");
}

type CgRecoveredCall = { name: string; arguments: string };

function cgParseDsml(rawText: string): Array<CgRecoveredCall> | null {
  const text = cgStripReasoning(rawText);
  const START = "<｜DSML｜function_calls>";
  const END = "</｜DSML｜function_calls>";
  const start = text.indexOf(START);
  const end = text.indexOf(END);
  if (start === -1 || end === -1 || end < start) {
    return null;
  }
  const body = text.slice(start + START.length, end);
  const invokeRe = /<｜DSML｜invoke name="([^"]*)">([\s\S]*?)<\/｜DSML｜invoke>/g;
  const paramRe = /<｜DSML｜parameter name="([^"]*)" string="(true|false)">([\s\S]*?)<\/｜DSML｜parameter>/g;
  const calls: Array<CgRecoveredCall> = [];
  let invokeMatch: RegExpExecArray | null;
  while ((invokeMatch = invokeRe.exec(body)) !== null) {
    const name = invokeMatch[1];
    const paramsText = invokeMatch[2];
    const args: Record<string, unknown> = {};
    paramRe.lastIndex = 0;
    let paramMatch: RegExpExecArray | null;
    let sawParam = false;
    while ((paramMatch = paramRe.exec(paramsText)) !== null) {
      sawParam = true;
      const pname = paramMatch[1];
      const isLiteralString = paramMatch[2] === "true";
      const raw = paramMatch[3];
      if (isLiteralString) {
        args[pname] = pname === "command" ? ["bash", "-lc", raw] : raw;
      } else {
        try {
          args[pname] = JSON.parse(raw);
        } catch {
          return null;
        }
      }
    }
    if (!name || !sawParam) {
      return null;
    }
    calls.push({ name, arguments: JSON.stringify(args) });
  }
  return calls.length > 0 ? calls : null;
}

function cgLogRecovery(record: Record<string, unknown>): void {
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const fs = require("fs");
    fs.appendFileSync("/output/dsml_recoveries.jsonl", JSON.stringify(record) + "\n");
  } catch {
    return;
  }
}

"""

EDITS = [
    (
        ADAPTER,
        "async function* streamResponses(\n",
        HELPERS + "async function* streamResponses(\n",
    ),
    (
        ADAPTER,
        """        const item = {
          type: "message",
          id: outputItemId,
          status: "completed",
          role: "assistant",
          content: [
            { type: "output_text", text: textContent, annotations: [] },
          ],
        };
        yield {
          type: "response.output_item.done",
          output_index: 0,
          item,
        };
        finalOutputItem.push(item as unknown as ResponseContentOutput);""",
        """        const item = {
          type: "message",
          id: outputItemId,
          status: "completed",
          role: "assistant",
          content: [
            { type: "output_text", text: textContent, annotations: [] },
          ],
        };
        yield {
          type: "response.output_item.done",
          output_index: 0,
          item,
        };
        finalOutputItem.push(item as unknown as ResponseContentOutput);
        if (toolCalls.size === 0) {
          const cgRecovered = cgParseDsml(textContent);
          if (cgRecovered) {
            finalOutputItem.pop();
            for (const cgCall of cgRecovered) {
              const cgCallId = generateId("call");
              const cgItem = {
                type: "function_call",
                id: outputItemId,
                status: "completed",
                call_id: cgCallId,
                name: cgCall.name,
                arguments: cgCall.arguments,
              };
              finalOutputItem.push(cgItem as unknown as ResponseContentOutput);
              toolCalls.set(toolCalls.size, { id: cgCallId, name: cgCall.name, arguments: cgCall.arguments });
            }
            cgLogRecovery({ time: new Date().toISOString(), calls: cgRecovered });
          }
        }""",
    ),
]

root = Path(sys.argv[1])
for relative, old, new in EDITS:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"{relative}: anchor not found exactly once ({text.count(old)}): {old[:70]!r}")
    path.write_text(text.replace(old, new), encoding="utf-8")
    print(f"patched {relative}")
