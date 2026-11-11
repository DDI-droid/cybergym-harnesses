"""Skip the update-check spawn site unconditionally in quiet mode (on top of it already being unreachable
under --network none). Applied after W2_patch_exec.py. usage: W2_patch_skip_updates.py <codex-cli/src dir>
"""
import sys
from pathlib import Path

root = Path(sys.argv[1])
path = root / "cli.tsx"
text = path.read_text(encoding="utf-8")
old = (
    "try {\n"
    "  await checkForUpdates();\n"
    "} catch {\n"
    "  // ignore\n"
    "}\n"
)
new = (
    "// Skipped entirely in quiet mode: it ends in detectInstallerByPath(), an execFileSync('npm', ...) call that\n"
    "// is not routed through cg_run. Under --network none it is already unreachable (the registry lookup fails\n"
    "// immediately), but quiet mode never needs the nag anyway, so it is never even attempted here.\n"
    "if (!cli.flags.quiet) {\n"
    "  try {\n"
    "    await checkForUpdates();\n"
    "  } catch {\n"
    "    // ignore\n"
    "  }\n"
    "}\n"
)
if text.count(old) != 1:
    raise SystemExit(f"anchor not found exactly once ({text.count(old)})")
path.write_text(text.replace(old, new), encoding="utf-8")
print(f"patched {path}")
