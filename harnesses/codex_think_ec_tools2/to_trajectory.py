import json
import sys
from pathlib import Path


def load_items(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if isinstance(document, dict) and "items" in document:
        return document["items"] or []
    items = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            items.append(item)
    return items


def convert(item: dict, index: int) -> dict:
    kind = item.get("type")
    step = {"step": index, "type": kind}
    if kind == "message":
        step["role"] = item.get("role")
        step["content"] = "\n".join(part.get("text", "") for part in item.get("content") or [] if isinstance(part, dict))
    elif kind == "function_call":
        step["name"] = item.get("name")
        step["arguments"] = item.get("arguments")
    elif kind == "function_call_output":
        step["output"] = item.get("output")
    else:
        step["item"] = item
    return step


items = load_items(Path(sys.argv[1]))
with open(sys.argv[2], "w", encoding="utf-8") as out:
    for index, item in enumerate(items, start=1):
        out.write(json.dumps(convert(item, index), ensure_ascii=False) + "\n")
