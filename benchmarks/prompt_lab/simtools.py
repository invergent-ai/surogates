"""Local stand-ins for the Surogates tools, with the harness's result shapes.

File and terminal tools act on a real per-run workspace directory, so a
scenario's checks can inspect what the agent produced. Web, KB, memory,
ask_user_question, artifacts and the rest are scripted per scenario.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

# The sandbox image's tools, as far as this host has them: LAB_TOOL_PATH goes
# in front of PATH (a venv with the python office libraries, node, ...), and
# LAB_NODE_PATH lets node find globally installed modules (pptxgenjs, docx).
TOOL_PATH = os.environ.get("LAB_TOOL_PATH", str(Path(sys.executable).parent))
NODE_PATH = os.environ.get("LAB_NODE_PATH", "")
# Commands the stand-in sandbox refuses: privilege, remote shells, host-wide
# deletes, private networks. LAB_DENY_REGEX adds host-specific paths.
_DENY = (r"(\bsudo\b|\bssh\b|\bscp\b|rm\s+-rf\s+(/|~|\$HOME)\s*($|;|&)|\.\./\.\./\.\.|\bshutdown\b|\breboot\b|"
         r"\bmkfs\b|:\(\)\s*\{|\bkill\s+-9\s+-1\b|\b10\.\d+\.\d+\.|\b172\.(1[6-9]|2\d|3[01])\.|\b192\.168\.)")
DENY = re.compile(_DENY + (f"|{os.environ['LAB_DENY_REGEX']}" if os.environ.get("LAB_DENY_REGEX") else ""))


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


class SimTools:
    def __init__(self, ws: Path, scenario: dict, skills_dir: Path, skills_index: dict):
        self.ws = ws
        self.sc = scenario
        self.skills_dir = skills_dir
        self.skills_index = skills_index  # name -> (dir Path, description)
        self.read_paths: set[str] = set()
        self.todos: list[dict] = []
        self.memory: dict[str, list[str]] = {"memory": [], "user": []}
        self.artifacts: list[dict] = []
        self.questions: list[dict] = []
        self.skill_loads: list[str] = []
        self.web_queries: list[str] = []
        self.answers = list(scenario.get("answers", []))
        self.custom_calls: list = []
        self.group_catalog: dict = {}
        self.browser = {"url": None, "fields": {}, "history": []}
        self.state: dict = {}

    # ------------------------------------------------------------ helpers
    def _path(self, p: str) -> Path:
        p = (p or ".").strip()
        if p.startswith("/workspace"):
            p = p[len("/workspace"):].lstrip("/") or "."
        if p.startswith("~/"):
            p = p[2:]
        if p in ("~", "$HOME"):
            p = "."
        q = (self.ws / p).resolve() if not p.startswith("/") else Path(p).resolve()
        if not str(q).startswith(str(self.ws.resolve())):
            raise PermissionError(f"Path outside workspace: {p}")
        return q

    def _rel(self, q: Path) -> str:
        return str(q.resolve().relative_to(self.ws.resolve()))

    # ------------------------------------------------------------ dispatch
    def call(self, name: str, args: dict) -> str:
        handler = (self.sc.get("handlers") or {}).get(name)
        if handler is not None:
            try:
                out = handler(args, self)
            except Exception as e:  # noqa: BLE001
                out = {"success": False, "error": f"{type(e).__name__}: {e}"}
            self.custom_calls.append((name, args))
            return out if isinstance(out, str) else _j(out)
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            return self.generic(name, args)
        try:
            return fn(**args) if isinstance(args, dict) else fn()
        except TypeError as e:
            return _j({"success": False, "error": f"Invalid arguments for {name}: {e}"})
        except PermissionError as e:
            return _j({"success": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            return _j({"success": False, "error": f"{type(e).__name__}: {e}"})

    def generic(self, name: str, args: dict) -> str:
        canned = (self.sc.get("tool_results") or {}).get(name)
        if canned is not None:
            return canned if isinstance(canned, str) else _j(canned)
        return _j({"success": False, "error": f"{name} is not available in this environment right now."})

    # ------------------------------------------------------------ files
    def t_read_file(self, path, offset=1, limit=2000, **_):
        q = self._path(path)
        if not q.exists():
            sims = [self._rel(x) for x in self.ws.rglob("*") if x.is_file() and Path(path).name.split(".")[0].lower() in x.name.lower()][:5]
            return _j({"success": False, "error": f"File not found: {path}", "similar_files": sims})
        if q.is_dir():
            return _j({"success": False, "error": f"{path} is a directory; use list_files"})
        self.read_paths.add(self._rel(q))
        suf = q.suffix.lower()
        if suf in (".xlsx", ".docx", ".pptx", ".pdf"):
            text = _doc_text(q)
        else:
            text = q.read_text(errors="replace")
        lines = text.splitlines()
        offset = max(1, int(offset or 1)); limit = min(2000, int(limit or 2000))
        chunk = lines[offset - 1: offset - 1 + limit]
        body = "\n".join(chunk)
        out = {"content": body, "total_lines": len(lines), "file_size": q.stat().st_size,
               "truncated": offset - 1 + limit < len(lines)}
        if out["truncated"]:
            out["next_offset"] = offset + limit
        return _j(out)

    def t_write_file(self, path, content, **_):
        q = self._path(path)
        rel = str(q.relative_to(self.ws.resolve())) if str(q).startswith(str(self.ws.resolve())) else path
        if q.exists() and rel not in self.read_paths:
            return _j({"success": False, "error": f"Refusing to overwrite {path}: read it first with read_file in this session (a blind overwrite discards content you never saw)."})
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text(content)
        self.read_paths.add(rel)
        return _j({"status": "ok", "path": path, "bytes_written": len(content.encode()), "lines_written": content.count("\n") + 1})

    def t_patch(self, mode="replace", path=None, old_string=None, new_string=None, replace_all=False, patch=None, **_):
        if mode == "patch":
            return _j({"success": False, "error": "V4A patch mode is not supported in this environment; use mode='replace'."})
        if not path or old_string is None or new_string is None:
            return _j({"success": False, "error": "replace mode needs path, old_string and new_string"})
        q = self._path(path)
        if not q.exists():
            return _j({"success": False, "error": f"File not found: {path}"})
        text = q.read_text()
        n = text.count(old_string)
        if n == 0:
            return _j({"success": False, "error": "old_string not found in file. Read the file again and copy the exact text."})
        if n > 1 and not replace_all:
            return _j({"success": False, "error": f"old_string matches {n} places; add context to make it unique or set replace_all=true."})
        new = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        q.write_text(new)
        import difflib
        diff = "".join(difflib.unified_diff(text.splitlines(True), new.splitlines(True), fromfile=path, tofile=path))
        return _j({"success": True, "diff": diff[:4000], "replacements": n if replace_all else 1})

    def t_list_files(self, path=".", pattern="*", limit=50, **_):
        q = self._path(path)
        files = sorted([x for x in q.rglob(pattern or "*") if x.is_file() and ".skills" not in x.parts],
                       key=lambda x: -x.stat().st_mtime)
        m = [self._rel(x) for x in files]
        return _j({"matches": m[:limit], "count": min(len(m), limit), "total_count": len(m),
                   "truncated": len(m) > limit, "pattern": pattern, "path": path})

    def t_search_files(self, pattern, target="content", path=".", file_glob=None, limit=50, offset=0,
                       output_mode="content", context=0, **_):
        q = self._path(path)
        if target == "files":
            return self.t_list_files(path=path, pattern=pattern, limit=limit)
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return _j({"success": False, "error": f"Invalid regex: {e}"})
        files = [q] if q.is_file() else [x for x in q.rglob(file_glob or "*") if x.is_file() and ".skills" not in x.parts]
        matches = []; counts = {}
        for f in files:
            try:
                lines = f.read_text(errors="replace").splitlines()
            except Exception:
                continue
            for i, l in enumerate(lines, 1):
                if rx.search(l):
                    counts[self._rel(f)] = counts.get(self._rel(f), 0) + 1
                    item = {"file": str(f.resolve()), "line": i, "content": l[:500]}
                    if context:
                        item["before"] = [f"{j}|{lines[j-1][:300]}" for j in range(max(1, i - context), i)]
                        item["after"] = [f"{j}|{lines[j-1][:300]}" for j in range(i + 1, min(len(lines), i + context) + 1)]
                    matches.append(item)
        if output_mode == "files_only":
            return _j({"files": list(counts)[offset:offset + limit], "count": len(counts)})
        if output_mode == "count":
            return _j({"counts": counts, "total": sum(counts.values())})
        return _j({"matches": matches[offset:offset + limit], "count": len(matches), "truncated": len(matches) > offset + limit})

    # ------------------------------------------------------------ terminal
    def t_terminal(self, command, timeout=180, workdir=None, background=False, **_):
        if DENY.search(command):
            return _j({"output": "", "exit_code": 126, "error": "Command blocked by sandbox policy."})
        cwd = self.ws if not workdir else self._path(workdir)
        ws = str(self.ws.resolve())
        # The sandbox's workspace is /workspace; here it is this run's folder.
        command = re.sub(r"(?<![\w./-])/workspace(?=/|\b|$)", ws, command)
        # Keep searches inside this run's sandbox: in the real one, / holds
        # nothing else of interest, here it holds every other run.
        command = re.sub(r"\bfind\s+/(\s|$)", f"find {ws}\\1", command)
        env = {"HOME": ws, "PATH": f"{TOOL_PATH}:/usr/local/bin:/usr/bin:/bin",
               "LANG": "C.UTF-8", "TERM": "dumb", "PYTHONDONTWRITEBYTECODE": "1", "NODE_PATH": NODE_PATH}
        if background:
            return _j({"output": "", "exit_code": None, "error": "Background processes are not available in this sandbox; run in the foreground."})
        try:
            p = subprocess.run(["bash", "-c", command], cwd=cwd, env=env, capture_output=True, text=True,
                               timeout=min(int(timeout or 180), 240))
            out = (p.stdout + (("\n" + p.stderr) if p.stderr else "")).strip()
            if len(out) > 30000:
                out = out[:15000] + f"\n... [{len(out) - 30000} chars truncated] ...\n" + out[-15000:]
            return _j({"output": out, "exit_code": p.returncode, "error": None})
        except subprocess.TimeoutExpired:
            return _j({"output": "", "exit_code": 124, "error": f"Command timed out after {timeout}s"})

    def t_process(self, action="list", **_):
        return _j({"processes": [], "error": None if action == "list" else "No such process."})

    # ------------------------------------------------------------ skills
    def t_skills_list(self, category=None, **_):
        items = [{"name": n, "description": d} for n, (_, d) in sorted(self.skills_index.items())]
        return _j({"success": True, "skills": items, "count": len(items)})

    def t_skill_view(self, name, file_path=None, **_):
        if name not in self.skills_index:
            return _j({"success": False, "error": f"Skill '{name}' not found.", "available": sorted(self.skills_index)})
        sdir, desc = self.skills_index[name]
        if file_path:
            f = sdir / file_path
            if not f.is_file():
                return _j({"success": False, "error": f"File not found in skill: {file_path}"})
            return _j({"success": True, "name": name, "file": file_path, "content": f.read_text(errors="replace")})
        self.skill_loads.append(name)
        text = (sdir / "SKILL.md").read_text()
        body = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S)
        linked = sorted(str(p.relative_to(sdir)) for p in sdir.rglob("*") if p.is_file() and p.name != "SKILL.md")
        res = {"success": True, "name": name, "description": desc, "content": body,
               "linked_files": linked or None}
        if linked:
            dst = self.ws / ".skills" / name
            if not dst.exists():
                shutil.copytree(sdir, dst)
            staged = f"{self.ws.resolve()}/.skills/{name}/"
            res["staged_at"] = staged
            res["content"] = (f"[Skill files staged at {staged} — run its scripts and read its "
                              f"files from there.]\n\n") + body
        return _j(res)

    def t_skill_manage(self, **kw):
        return _j({"success": False, "error": "Skill changes are disabled for this agent."})

    # ------------------------------------------------------------ planning / memory
    def t_todo(self, todos=None, merge=False, **_):
        if todos is not None:
            if not merge:
                self.todos = [dict(t) for t in todos]
            else:
                byid = {t["id"]: t for t in self.todos}
                for t in todos:
                    if t.get("id") in byid:
                        byid[t["id"]].update(t)
                    else:
                        self.todos.append(dict(t))
        st = lambda s: sum(1 for t in self.todos if t.get("status") == s)
        return _j({"todos": self.todos, "summary": {"total": len(self.todos), "pending": st("pending"),
                   "in_progress": st("in_progress"), "completed": st("completed"), "cancelled": st("cancelled")}})

    def t_memory(self, action, target="memory", content=None, old_text=None, **_):
        store = self.memory.setdefault(target, [])
        if action == "add" and content:
            store.append(content)
        elif action in ("replace", "remove") and old_text:
            hit = [i for i, e in enumerate(store) if old_text in e]
            if not hit:
                return _j({"success": False, "error": "No entry matches old_text."})
            if action == "remove":
                store.pop(hit[0])
            else:
                store[hit[0]] = content or store[hit[0]]
        else:
            return _j({"success": False, "error": "Invalid memory call."})
        return _j({"success": True, "message": "Entry saved.", "entries": store})

    def t_session_search(self, query="", **_):
        return _j({"success": True, "query": query, "results": [], "count": 0, "message": "No matching sessions found."})

    def t_ask_user_question(self, questions=None, message=None, **_):
        self.questions.append({"questions": questions, "message": message})
        ans = self.answers.pop(0) if self.answers else "I can't answer right now — please use your best judgment and go ahead."
        qs = questions or [{"prompt": ""}]
        return _j({"cancelled": False, "responses": [{"question": q.get("prompt", ""), "answer": ans, "is_other": True} for q in qs]})

    def t_create_artifact(self, name=None, kind=None, spec=None, artifact_id=None, **_):
        if kind not in ("markdown", "table", "chart", "html", "svg"):
            return _j({"success": False, "error": f"Unknown kind {kind!r}"})
        need = {"markdown": "content", "table": "rows", "chart": "chart_js", "html": "html", "svg": "svg"}[kind]
        if not isinstance(spec, dict) or need not in spec:
            return _j({"success": False, "error": f"spec.{need} is required for kind={kind}"})
        aid = artifact_id or str(uuid.uuid4())
        self.artifacts.append({"name": name, "kind": kind, "spec": spec, "artifact_id": aid})
        return _j({"success": True, "artifact_id": aid, "name": name, "kind": kind, "version": 1})

    # ------------------------------------------------------------ tool groups
    def t_load_tools(self, groups=None, **_):
        groups = [groups] if isinstance(groups, str) else list(groups or [])
        loaded = [g for g in groups if g in self.group_catalog]
        if not loaded:
            return _j({"success": False, "error": f"No such tool group: {', '.join(map(str, groups)) or '(none given)'}.",
                       "groups": sorted(self.group_catalog)})
        out = {"success": True, "loaded": loaded, "message": "Loaded. Their tools are available from your next step."}
        guides = {g: self.group_catalog[g]["guide"] for g in loaded if self.group_catalog[g].get("guide")}
        if guides:
            out["guide"] = guides
        return _j(out)

    # ------------------------------------------------------------ browser (scripted site)
    def _page(self):
        p = (self.sc.get("site") or {}).get(self.browser["url"])
        return p(self) if callable(p) else p

    def _outline(self):
        p = self._page()
        if p is None:
            return f"# Error\nCould not load {self.browser['url']} (net::ERR_NAME_NOT_RESOLVED)"
        lines = [f"# {p['title']}"] + list(p.get("text", []))
        for e in p.get("elements", []):
            val = self.browser["fields"].get((self.browser["url"], e["ref"]))
            extra = f' value="{val}"' if val else ""
            lines.append(f'- {e["role"]} @{e["ref"]} "{e["name"]}"{extra}')
        return "\n".join(lines)

    def t_browser_navigate(self, url, **_):
        self.browser["url"] = url.rstrip("/") if url.rstrip("/") in (self.sc.get("site") or {}) else url
        self.browser["history"].append(("navigate", url))
        p = self._page()
        return _j({"success": p is not None, "url": self.browser["url"], "title": p["title"] if p else "", "snapshot": self._outline()})

    def t_browser_get_state(self, **_):
        return _j({"success": True, "url": self.browser["url"], "snapshot": self._outline()})

    def t_browser_click(self, ref=None, **_):
        p = self._page()
        if p is None:
            return _j({"success": False, "error": "No page open."})
        ref = (ref or "").lstrip("@")
        e = next((x for x in p.get("elements", []) if x["ref"] == ref), None)
        if e is None:
            return _j({"success": False, "error": f"No element @{ref} on this page; call browser_get_state."})
        self.browser["history"].append(("click", ref))
        if e.get("action"):
            nxt = e["action"](self)
            if nxt:
                self.browser["url"] = nxt
        elif e.get("href"):
            self.browser["url"] = e["href"]
        return _j({"success": True, "url": self.browser["url"], "snapshot": self._outline()})

    def t_browser_type(self, text=None, ref=None, **_):
        ref = (ref or "").lstrip("@")
        p = self._page()
        if p is None or not any(x["ref"] == ref for x in p.get("elements", [])):
            return _j({"success": False, "error": f"No field @{ref} on this page."})
        self.browser["fields"][(self.browser["url"], ref)] = text
        self.browser["history"].append(("type", ref, text))
        return _j({"success": True, "typed": text, "ref": "@" + ref})

    def t_browser_evaluate(self, code="", **_):
        p = self._page()
        return _j({"success": True, "result": (p or {}).get("text", [])})

    def t_browser_press_key(self, **_):
        return _j({"success": True})

    def t_browser_scroll(self, **_):
        return _j({"success": True, "at_bottom": True})

    def t_browser_wait(self, **_):
        return _j({"success": True})

    def t_browser_close(self, **_):
        self.browser["url"] = None
        return _j({"success": True})

    def t_browser_screenshot(self, **_):
        return _j({"success": True, "path": "media/screenshots/shot.png", "note": "Open it with read_file to see it."})

    # ------------------------------------------------------------ github / media
    def t_github(self, path="", method="GET", params=None, body=None, **_):
        gh = self.sc.get("github") or {}
        for pattern, resp in gh.items():
            if re.fullmatch(pattern, path or ""):
                return _j(resp(params or {}, body) if callable(resp) else resp)
        return _j({"success": False, "status": 404, "error": f"Not Found: {path}"})

    def t_generate_image(self, prompt="", output_path=None, aspect_ratio=None, **_):
        rel = output_path or "media/images/image-0001.png"
        q = self._path(rel)
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        self.state.setdefault("images", []).append({"prompt": prompt, "aspect_ratio": aspect_ratio, "path": rel})
        w, h = (1024, 1024) if aspect_ratio in (None, "1:1") else (1344, 768)
        return _j({"success": True, "path": rel, "width": w, "height": h, "delivery": "Shown in the chat automatically."})

    # ------------------------------------------------------------ web
    def t_web_search(self, query, limit=5, **_):
        self.web_queries.append(query)
        corpus = self.sc.get("web") or {}
        ql = query.lower()
        res = []
        for page in corpus.get("pages", []):
            score = sum(1 for k in page.get("keywords", []) if k.lower() in ql)
            if score:
                res.append((score, page))
        res.sort(key=lambda x: -x[0])
        out = [{"title": p["title"], "url": p["url"], "description": p.get("snippet", "")} for _, p in res[: int(limit or 5)]]
        return _j({"success": True, "data": {"web": out}})

    def t_web_extract(self, urls, **_):
        corpus = {p["url"]: p for p in (self.sc.get("web") or {}).get("pages", [])}
        results = []
        for u in urls if isinstance(urls, list) else [urls]:
            p = corpus.get(u)
            blocked = (self.sc.get("extract_blocked") or {})
            if any(u.startswith(b) for b in blocked):
                results.append({"url": u, "title": "", "content": "", "error": next(v for b, v in blocked.items() if u.startswith(b))})
                continue
            if p:
                results.append({"url": u, "title": p["title"], "content": p.get("content", p.get("snippet", ""))})
            else:
                results.append({"url": u, "title": "", "content": "", "error": "Failed to fetch: HTTP 404"})
        return _j({"results": results})

    # ------------------------------------------------------------ knowledge base
    def _kb(self):
        return (self.sc.get("kb") or {}).get("pages", {})

    def t_kb_search_pages(self, query="", kb_id=None, limit=5, **_):
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 3]
        scored = []
        for path, text in self._kb().items():
            tl = text.lower()
            s = sum(tl.count(w) for w in words)
            if s:
                scored.append((s, path, text))
        scored.sort(key=lambda x: -x[0])
        return _j({"success": True, "results": [{"kb_id": kb_id or "kb-handbook", "path": p, "title": t.splitlines()[0].lstrip("# "),
                    "snippet": t[:300]} for _, p, t in scored[: int(limit or 5)]]})

    def t_kb_list_pages(self, kb_id=None, **_):
        return _j({"success": True, "pages": [{"path": p, "title": t.splitlines()[0].lstrip("# ")} for p, t in self._kb().items()]})

    def t_kb_read_page(self, path=None, kb_id=None, **kw):
        path = path or kw.get("page_path") or kw.get("page")
        t = self._kb().get(path)
        if t is None:
            return _j({"success": False, "error": f"Page not found: {path}"})
        return _j({"success": True, "kb_id": kb_id or "kb-handbook", "path": path, "content": t})


def _doc_text(q: Path) -> str:
    suf = q.suffix.lower()
    try:
        if suf == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(q, data_only=False)
            out = []
            for ws in wb.worksheets:
                out.append(f"## Sheet: {ws.title}")
                for row in ws.iter_rows(values_only=True):
                    out.append(",".join("" if v is None else str(v) for v in row))
            return "\n".join(out)
        if suf == ".docx":
            import docx
            return "\n".join(p.text for p in docx.Document(q).paragraphs)
        if suf == ".pptx":
            import pptx
            out = []
            for i, s in enumerate(pptx.Presentation(q).slides, 1):
                out.append(f"## Slide {i}")
                for sh in s.shapes:
                    if sh.has_text_frame:
                        out.append(sh.text_frame.text)
            return "\n".join(out)
        if suf == ".pdf":
            import pypdf
            return "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(q).pages)
    except Exception as e:  # noqa: BLE001
        return f"[could not parse {q.name}: {e}]"
    return ""
