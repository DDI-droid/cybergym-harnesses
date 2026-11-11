from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

WORKSPACE = Path(os.environ.get("CYBERGYM_WORKSPACE", "/workspace"))
OUTPUT = Path(os.environ.get("CYBERGYM_OUTPUT", "/output"))


def settings() -> dict[str, Any]:
    return json.loads(os.environ.get("CYBERGYM_HARNESS_SETTINGS", "{}"))


def model() -> str:
    return os.environ["CYBERGYM_MODEL"]


def time_limit() -> int:
    return int(os.environ.get("CYBERGYM_TIME_LIMIT_S", "0"))


class Trajectory:
    def __init__(self, path: Path | None = None):
        self.path = path or OUTPUT / "trajectory.jsonl"
        self.started = time.time()

    def log(self, kind: str, **fields: Any) -> None:
        record = {"t": round(time.time() - self.started, 3), "type": kind, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")


def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, **params: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model(), "messages": messages, **params}
    if tools:
        body["tools"] = tools
    request = urllib.request.Request(
        os.environ["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read())


def submit(poc: Path) -> dict[str, Any]:
    completed = subprocess.run(
        ["bash", str(WORKSPACE / "submit.sh"), str(poc)], capture_output=True, text=True, timeout=300
    )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"error": (completed.stdout + completed.stderr)[-2000:]}


def save_final(poc: Path) -> None:
    shutil.copyfile(poc, OUTPUT / "poc")
