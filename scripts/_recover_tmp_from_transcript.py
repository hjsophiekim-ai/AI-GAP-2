"""Recover deleted tmp scripts from sibling agent transcript Write/StrReplace ops."""
from __future__ import annotations

import json
from pathlib import Path

TRANSCRIPT = Path(
    r"C:\Users\FURSYS\.cursor\projects\c-Users-FURSYS-Desktop-AI-GAP-2"
    r"\agent-transcripts\6d5a6f36-dd66-48fd-a70c-9656d41a26aa"
    r"\subagents\e5b26156-f2c6-44b6-af7e-210cb33f837e.jsonl"
)
OUT_DIR = Path(__file__).resolve().parent
WANTED = {
    "_tmp_replay_jul21_shaped.py",
    "_tmp_diagnose_jul21.py",
    "_tmp_replay_jul21_naver.py",
}


def main() -> int:
    last_write: dict[str, tuple[int, str]] = {}
    lines = list(TRANSCRIPT.open(encoding="utf-8"))
    for i, line in enumerate(lines):
        obj = json.loads(line)
        content = (obj.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not (
                isinstance(c, dict)
                and c.get("type") == "tool_use"
                and c.get("name") == "Write"
            ):
                continue
            inp = c.get("input") or {}
            name = Path(inp.get("path") or "").name
            if name in WANTED and inp.get("contents"):
                last_write[name] = (i, inp["contents"])
                print(f"Write {name} at {i} len={len(inp['contents'])}")

    for name, (write_i, contents) in last_write.items():
        for j, line in enumerate(lines):
            if j <= write_i:
                continue
            obj = json.loads(line)
            content = (obj.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if not (
                    isinstance(c, dict)
                    and c.get("type") == "tool_use"
                    and c.get("name") == "StrReplace"
                ):
                    continue
                inp = c.get("input") or {}
                if Path(inp.get("path") or "").name != name:
                    continue
                old, new = inp.get("old_string"), inp.get("new_string")
                if old is None or new is None:
                    continue
                if old not in contents:
                    print(f"WARN StrReplace miss {name} line {j}")
                    continue
                contents = contents.replace(old, new, 1)
                print(f"Applied StrReplace {name} line {j}")
        dest = OUT_DIR / name
        dest.write_text(contents, encoding="utf-8")
        print(f"Wrote {dest} final_len={len(contents)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
