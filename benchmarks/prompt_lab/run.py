"""Run scenarios through a Surogates-shaped agent loop against our model.

  python run.py --variant base=/path/to/checkout-a --variant opt=/path/to/checkout-b@surogate/Surogate3.7-35B-A3B \
      --samples 3 --out runs/r1 [--only id1,id2]

Environment: LAB_BASE_URLS (comma-separated OpenAI-compatible base URLs; one is
picked per conversation), LAB_API_KEY, LAB_MODEL, LAB_PYTHON (an interpreter
with the product's dependencies; it renders each checkout with
PYTHONPATH=<checkout>).

Each variant is a checkout: its system prompt and tool schemas are rendered
by that checkout's own PromptBuilder/ToolRegistry (render.py), and
skill_view serves that checkout's skills/.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from simtools import SimTools  # noqa: E402
import scenarios as SC  # noqa: E402

LAB = Path(__file__).resolve().parent
PY = os.environ.get("LAB_PYTHON", sys.executable)
MODEL = os.environ.get("LAB_MODEL", "surogate/Surogate3.7-35B-A3B")
KEY = os.environ.get("LAB_API_KEY", "")
BASE_URLS = [u.strip() for u in os.environ.get("LAB_BASE_URLS", "https://llm.surogate.ai:8443/v1").split(",") if u.strip()]
MAX_ITERS = 40
AUTOREPLY = "I'm not available to answer questions right now. Please go ahead with your best judgment and finish the task."

_render_cache: dict = {}


def render(repo: str, profile: str) -> dict:
    """repo may carry '@<model id>' (default: the 'surogate' tier sentinel)."""
    k = (repo, profile)
    if k not in _render_cache:
        path, _, model = repo.partition("@")
        env = dict(os.environ, PYTHONPATH=str(Path(path).resolve()))
        out = subprocess.run([PY, str(LAB / "render.py"), profile, model or "surogate"], env=env, capture_output=True, text=True, check=True)
        _render_cache[k] = json.loads(out.stdout)
    return _render_cache[k]


def skills_index(repo: str) -> dict:
    root = Path(repo) / "skills"
    idx = {}
    for sk in root.rglob("SKILL.md"):
        text = sk.read_text()
        m = re.search(r"^name:\s*(.+)$", text, re.M)
        d = re.search(r"^description:\s*(.+)$", text, re.M)
        name = (m.group(1).strip().strip("'\"") if m else sk.parent.name)
        idx[name] = (sk.parent, d.group(1).strip() if d else "")
    return idx


class Endpoints:
    def __init__(self, urls):
        self.cycle = itertools.cycle(urls)

    def next(self):
        return next(self.cycle)


async def chat(client: httpx.AsyncClient, url: str, payload: dict) -> dict:
    for attempt in range(4):
        try:
            r = await client.post(url + "/chat/completions", json=payload,
                                  headers={"Authorization": f"Bearer {KEY}"} if KEY else {}, timeout=900)
            if r.status_code == 200:
                return r.json()
            err = f"HTTP {r.status_code}: {r.text[:300]}"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        await asyncio.sleep(5 * (attempt + 1))
    raise RuntimeError(err)


async def run_one(client, eps: Endpoints, variant: str, repo: str, sc: dict, sample: int, out: Path, sem):
    async with sem:
        rdir = out / variant / sc["id"] / f"s{sample}"
        if (rdir / "result.json").exists():
            return json.loads((rdir / "result.json").read_text())
        ws = rdir / "ws"
        if ws.exists():
            shutil.rmtree(ws)
        ws.mkdir(parents=True)
        for rel, content in (sc.get("files") or {}).items():
            p = ws / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                p.write_bytes(content)
            elif callable(content):
                content(p)
            else:
                p.write_text(content)
        r = render(repo, sc.get("profile", "studio"))
        repo_path = repo.partition("@")[0]
        tools = SimTools(ws, sc, Path(repo_path) / "skills", skills_index(repo_path))
        system = r["system"]
        all_tools = r["tools"] + list(sc.get("extra_tools") or [])
        messages = [{"role": "system", "content": system}]
        turns = list(sc["turns"]) if "turns" in sc else [sc["prompt"]]
        messages.append({"role": "user", "content": turns.pop(0)})
        log = {"variant": variant, "scenario": sc["id"], "sample": sample, "requests": []}
        t0 = time.time()
        autoreplies = 0
        bad_calls = 0
        unknown_tools = 0
        tool_names = {t["function"]["name"] for t in r["tools"]} | {t["function"]["name"] for t in sc.get("extra_tools") or []}
        calls = []
        finals = []
        error = None
        url = eps.next()  # one engine per conversation, like the F5's source persistence (prefix cache)
        for it in range(MAX_ITERS):
            payload = {"model": MODEL, "messages": messages, "tools": all_tools}
            payload.update(sc.get("sampling") or {})
            ts = time.time()
            try:
                resp = await chat(client, url, payload)
            except Exception as e:  # noqa: BLE001
                error = str(e)
                break
            ch = resp["choices"][0]
            msg = ch["message"]
            usage = resp.get("usage") or {}
            reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
            log["requests"].append({"it": it, "dt": round(time.time() - ts, 2), "usage": usage,
                                    "finish": ch.get("finish_reason"), "content": msg.get("content"),
                                    "reasoning_chars": len(reasoning),
                                    "tool_calls": msg.get("tool_calls")})
            am = {"role": "assistant", "content": msg.get("content") or ""}
            if reasoning:
                am["reasoning_content"] = reasoning
            tcs = msg.get("tool_calls") or []
            if tcs:
                am["tool_calls"] = tcs
            messages.append(am)
            if tcs:
                for tc in tcs:
                    fn = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"].get("arguments") or "{}")
                        if not isinstance(args, dict):
                            raise ValueError("arguments not an object")
                    except Exception as e:  # noqa: BLE001
                        bad_calls += 1
                        res = json.dumps({"success": False, "error": f"Invalid JSON arguments: {e}"})
                        args = None
                    if args is not None:
                        if fn not in tool_names:
                            unknown_tools += 1
                            res = json.dumps({"success": False, "error": f"Unknown tool: {fn}"})
                        else:
                            res = await asyncio.to_thread(tools.call, fn, args)
                    calls.append({"name": fn, "args": args, "result": res[:3000]})
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": res})
                if fn == "ask_user_question":
                    pass
                continue
            text = msg.get("content") or ""
            finals.append(text)
            if turns:
                messages.append({"role": "user", "content": turns.pop(0)})
                continue
            if autoreplies < sc.get("max_autoreplies", 1) and _wants_reply(text, sc):
                autoreplies += 1
                messages.append({"role": "user", "content": sc.get("autoreply") or AUTOREPLY})
                continue
            break
        res = {
            "variant": variant, "scenario": sc["id"], "sample": sample,
            "iters": len(log["requests"]), "wall": round(time.time() - t0, 1), "error": error,
            "prompt_tokens_first": (log["requests"][0]["usage"].get("prompt_tokens") if log["requests"] else None),
            "prompt_tokens_total": sum(q["usage"].get("prompt_tokens", 0) for q in log["requests"]),
            "completion_tokens": sum(q["usage"].get("completion_tokens", 0) for q in log["requests"]),
            "autoreplies": autoreplies, "bad_calls": bad_calls, "unknown_tools": unknown_tools,
            "tool_calls": [c["name"] for c in calls], "skill_loads": tools.skill_loads,
            "questions": len(tools.questions), "final": finals[-1] if finals else "",
            "finals": finals, "artifacts": len(tools.artifacts), "memory": tools.memory,
            "todos": len(tools.todos), "batched_rounds": sum(1 for q in log["requests"] if q["tool_calls"] and len(q["tool_calls"]) > 1),
            "hit_cap": len(log["requests"]) >= MAX_ITERS,
        }
        ctx = {"ws": ws, "res": res, "calls": calls, "tools": tools, "messages": messages}
        try:
            score, notes = (0.0, "error") if error else sc["check"](ctx)
        except Exception as e:  # noqa: BLE001
            score, notes = 0.0, f"check crashed: {type(e).__name__}: {e}"
        res["score"] = float(score)
        res["notes"] = notes
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "transcript.json").write_text(json.dumps({"messages": messages, "log": log, "calls": calls}, ensure_ascii=False, indent=1))
        (rdir / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
        print(f"{variant:10} {sc['id']:28} s{sample} score={res['score']:.2f} it={res['iters']:2} "
              f"tok1={res['prompt_tokens_first']} wall={res['wall']}s {notes[:90]}", flush=True)
        return res


def _wants_reply(text: str, sc: dict) -> bool:
    """True when the turn ended by handing a question/decision to the user."""
    if sc.get("no_autoreply"):
        return False
    t = text.strip()
    if not t:
        return True
    tail = t.rstrip("*_ )`\n")
    if tail.endswith("?"):
        return True
    return bool(re.search(r"(would you like|do you want|shall i|should i|let me know|please (provide|share|confirm|upload|send|tell|clarify)|"
                          r"could you (provide|share|confirm|tell|upload|clarify)|can you (provide|share|confirm|upload|clarify))",
                          t[-500:].lower()))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", action="append", required=True, help="name=repo_path")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--skip", default="")
    ap.add_argument("--concurrency", type=int, default=24)
    a = ap.parse_args()
    variants = [v.split("=", 1) for v in a.variant]
    scs = SC.SCENARIOS
    if a.only:
        keep = set(a.only.split(","))
        scs = [s for s in scs if s["id"] in keep]
    if a.skip:
        drop = set(a.skip.split(","))
        scs = [s for s in scs if s["id"] not in drop]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, repo in variants:
        for prof in {s.get("profile", "studio") for s in scs}:
            r = render(repo, prof)
            (out / f"render_{name}_{prof}.json").write_text(json.dumps(r, ensure_ascii=False))
    eps = Endpoints(BASE_URLS)
    sem = asyncio.Semaphore(a.concurrency)
    jobs = [(name, repo, sc, k) for k in range(a.samples) for sc in scs for name, repo in variants]
    random.Random(0).shuffle(jobs)
    limits = httpx.Limits(max_connections=a.concurrency * 2)
    async with httpx.AsyncClient(limits=limits, verify=True) as client:
        results = await asyncio.gather(*[run_one(client, eps, n, r, sc, k, out, sem) for n, r, sc, k in jobs])
    (out / "results.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in results) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
