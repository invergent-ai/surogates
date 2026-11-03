"""Re-apply the current scenario checks to finished runs (no model calls).

  ../venv/bin/python rescore.py runs/X [runs/Y ...]
Rebuilds the tool state the checks read (memory, artifacts, questions,
skill loads, read paths) from the saved tool calls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenarios as SC  # noqa: E402


class State:
    def __init__(self, calls):
        self.memory = {"memory": [], "user": []}
        self.artifacts, self.questions, self.skill_loads, self.web_queries = [], [], [], []
        self.read_paths = set()
        self.todos = []
        for c in calls:
            a = c.get("args") or {}
            n = c["name"]
            ok = '"success": false' not in (c.get("result") or "")
            if n == "memory" and a.get("action") == "add" and ok:
                self.memory.setdefault(a.get("target", "memory"), []).append(a.get("content", ""))
            elif n == "create_artifact" and ok:
                self.artifacts.append({"name": a.get("name"), "kind": a.get("kind"), "spec": a.get("spec"), "artifact_id": a.get("artifact_id")})
            elif n == "ask_user_question":
                self.questions.append(a)
            elif n == "skill_view" and not a.get("file_path") and ok:
                self.skill_loads.append(a.get("name"))
            elif n == "read_file" and ok:
                self.read_paths.add(a.get("path"))
            elif n == "web_search":
                self.web_queries.append(a.get("query"))
            elif n == "todo" and a.get("todos"):
                self.todos = a["todos"]


def main():
    by_id = {s["id"]: s for s in SC.SCENARIOS}
    for d in sys.argv[1:]:
        n = 0
        for rf in Path(d).glob("*/*/s*/result.json"):
            res = json.loads(rf.read_text())
            sc = by_id.get(res["scenario"])
            if sc is None or res.get("error"):
                continue
            tr = json.loads((rf.parent / "transcript.json").read_text())
            ctx = {"ws": rf.parent / "ws", "res": res, "calls": tr["calls"], "tools": State(tr["calls"]), "messages": tr["messages"]}
            try:
                score, notes = sc["check"](ctx)
            except Exception as e:  # noqa: BLE001
                score, notes = 0.0, f"check crashed: {type(e).__name__}: {e}"
            res.setdefault("score_orig", res["score"])
            res["score"], res["notes"] = float(score), notes
            rf.write_text(json.dumps(res, ensure_ascii=False, indent=1))
            n += 1
        print(d, "rescored", n)


if __name__ == "__main__":
    main()
