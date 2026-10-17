"""Check primitives: small factories that each return one named check.

A check reads the saved document (the model from ``docs.py``) and returns
``(passed, detail)``: ``True``/``False``, or ``None`` when it cannot tell
(a formula this evaluator does not implement, a file that stores no
computed values). ``None`` is reported, never counted as a pass or a fail.

``claim`` lists words that, said in the agent's final message, claim what
the check verifies: a failed check whose words the agent used is a claim
the document contradicts.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from obench import docs
from obench.docs import Deck, PdfDoc, SheetBook, WordDoc, norm


@dataclass
class Ctx:
    original: Any = None  # the input document's model, for an edit task
    final_message: str = ""
    inputs_changed: list[str] = field(default_factory=list)  # input names whose bytes changed


@dataclass
class CheckResult:
    name: str
    passed: bool | None
    detail: str
    claim: tuple[str, ...] = ()


Fn = Callable[[Any, Ctx], tuple[bool | None, str]]


@dataclass(frozen=True)
class Check:
    name: str
    fn: Fn
    claim: tuple[str, ...] = ()

    def run(self, doc: Any, ctx: Ctx) -> CheckResult:
        try:
            passed, detail = self.fn(doc, ctx)
        except Exception as exc:  # noqa: BLE001 - a check that crashes says so, it does not stop the others
            passed, detail = None, f"check crashed: {type(exc).__name__}: {exc}"
        return CheckResult(self.name, passed, detail, self.claim)


def check(name: str, *claim: str):
    def wrap(fn: Fn) -> Check:
        return Check(name, fn, tuple(claim))
    return wrap


# --------------------------------------------------------------------------- text and numbers


def num_pattern(value: float | int | str) -> str:
    """A regex for a number however a person or an editor writes it:
    12400 matches 12,400 / 12.400 / 12 400 / 12400; 4.4 matches 4,4."""
    text = str(value)
    if re.fullmatch(r"\d+", text) and len(text) > 3:
        groups = []
        while text:
            groups.insert(0, text[-3:])
            text = text[:-3]
        return r"(?<![\d.,])" + r"[,.\s  ]?".join(groups) + r"(?![\d]|[.,]\d)"
    if re.fullmatch(r"\d+\.\d+", text):
        a, b = text.split(".")
        return rf"(?<![\d.,]){a}[.,]{b}(?!\d)"
    return rf"(?<![\d.,]){re.escape(text)}(?![\d]|[.,]\d)"


def has_text(text: str, item: str | tuple[str, ...]) -> bool:
    """*item* is a phrase, a number (as int/float), or a tuple of alternatives."""
    options = item if isinstance(item, tuple) else (item,)
    hay = norm(text)
    for opt in options:
        if isinstance(opt, (int, float)) or re.fullmatch(r"[\d.]+", str(opt)):
            if re.search(num_pattern(opt), hay):
                return True
        elif norm(opt) in hay:
            return True
    return False


def contains_all(items: list, name: str = "", text_of: Callable[[Any], str] = lambda d: d.text()) -> Check:
    def fn(doc, ctx):
        text = text_of(doc)
        missing = [i for i in items if not has_text(text, i)]
        return (not missing, "all present" if not missing else f"missing: {missing}")
    return Check(name or f"mentions {len(items)} key facts", fn)


# --------------------------------------------------------------------------- Word


def w_headings(minimum: int) -> Check:
    def fn(doc: WordDoc, ctx):
        hs = [p.text for p in doc.headings if p.text.strip()]
        return len(hs) >= minimum, f"{len(hs)} headings: {hs[:8]}"
    return Check(f"at least {minimum} headings (heading styles)", fn, ("heading", "section"))


def w_list(kind: str, minimum: int) -> Check:
    def fn(doc: WordDoc, ctx):
        items = [p.text for p in doc.paragraphs if p.list_kind == kind and p.text.strip()]
        return len(items) >= minimum, f"{len(items)} {kind} items" + (f": {items[:6]}" if items else "")
    noun = "bulleted" if kind == "bullet" else "numbered"
    return Check(f"a {noun} list of at least {minimum} items", fn, ("bullet",) if kind == "bullet" else ("numbered", "steps"))


def w_table(min_rows: int, min_cols: int, containing: list | None = None, count: int = 1, label: str = "") -> Check:
    def fn(doc: WordDoc, ctx):
        ok, closest = [], None
        for t in doc.tables:
            if len(t.rows) >= min_rows and max((len(r) for r in t.rows), default=0) >= min_cols:
                text = "\n".join(" | ".join(r) for r in t.rows)
                missing = [c for c in (containing or []) if not C_has(text, c)]
                if not missing:
                    ok.append(t)
                elif closest is None or len(missing) < len(closest):
                    closest = missing
        shapes = [f"{len(t.rows)}x{max((len(r) for r in t.rows), default=0)}" for t in doc.tables]
        detail = f"tables {shapes}; {len(ok)} match"
        if len(ok) < count and closest:
            detail += f"; the closest lacks {closest}"
        return len(ok) >= count, detail
    what = f" holding {containing}" if containing else ""
    return Check(label or f"a table of at least {min_rows}x{min_cols}{what}", fn, ("table",))


def C_has(text: str, item) -> bool:
    return has_text(text, item)


def w_chart() -> Check:
    def fn(doc: WordDoc, ctx):
        if doc.charts:
            types = [docs.chart_info(x)[0] for x in doc.chart_xml]
            return True, f"{doc.charts} native chart(s) {types}"
        return False, f"no chart part ({doc.pictures} picture(s))"
    return Check("a native chart", fn, ("chart", "graph"))


def w_words(lo: int, hi: int) -> Check:
    def fn(doc: WordDoc, ctx):
        n = doc.body_words()
        return lo <= n <= hi, f"{n} words"
    return Check(f"length {lo}-{hi} words", fn, ("page",))


def w_no_pending() -> Check:
    def fn(doc: WordDoc, ctx):
        return not doc.pending, "none left" if not doc.pending else f"unfilled pending blocks: {doc.pending}"
    return Check("no unfilled pending block", fn)


def w_sig(block) -> tuple:
    if isinstance(block, docs.Table):
        return ("table", tuple(tuple(norm(c) for c in r) for r in block.rows))
    return ("p", norm(block.text), block.heading, block.list_kind, block.number)


def w_keeps_rest(changed: Callable[[Any], bool] = lambda b: False, name: str = "the rest of the document is kept") -> Check:
    """Every original block the task did not ask to change is still there, in order, with its text, heading level and
    list kind."""
    def fn(doc: WordDoc, ctx):
        if ctx.original is None:
            return None, "no original to compare"
        want = [w_sig(b) for b in ctx.original.blocks if not changed(b) and not (isinstance(b, docs.Para) and not b.text.strip())]
        have = [w_sig(b) for b in doc.blocks]
        missing, pos = [], 0
        for w in want:
            # Search forward from the last match; a block not found does not consume the rest.
            try:
                pos = have.index(w, pos) + 1
            except ValueError:
                missing.append(w)
        if not missing:
            return True, f"all {len(want)} untouched blocks kept in order"
        have_set = set(have)
        lost = [m for m in missing if m not in have_set]
        moved = [m for m in missing if m in have_set]
        out = []
        if lost:
            out.append(f"changed or lost: {[(m[1] + (f' (was item {m[4]})' if m[4] else '')) if m[0] == 'p' else 'table' for m in lost][:5]}")
        if moved:
            out.append(f"out of order: {[m[1] for m in moved][:5]}")
        return False, "; ".join(out)
    return Check(name, fn)


def w_bold(phrase: str, bold: bool = True) -> Check:
    def fn(doc: WordDoc, ctx):
        for p in doc.paragraphs:
            text = "".join(r.text for r in p.runs)
            i = text.lower().find(phrase.lower())
            if i < 0:
                continue
            pos, states = 0, []
            for r in p.runs:
                s, e = pos, pos + len(r.text)
                pos = e
                if e > i and s < i + len(phrase) and r.text.strip():
                    states.append(r.bold)
            ok = bool(states) and all(st == bold for st in states)
            return ok, f"runs over the phrase bold={states}"
        return False, f"phrase {phrase!r} not found"
    return Check(f"{phrase!r} is {'bold' if bold else 'not bold'}", fn, ("bold",) if bold else ())


# --------------------------------------------------------------------------- Spreadsheet


def x_numbers(values: list[float]) -> Check:
    def fn(book: SheetBook, ctx):
        found = set()
        ev = book.evaluator()
        for s in book.sheets:
            for ref, c in book.cells[s].items():
                v = c.raw
                if isinstance(v, str) and v.startswith("="):
                    continue
                for want in values:
                    try:
                        if abs(float(v) - want) < 1e-6:
                            found.add(want)
                    except (TypeError, ValueError):
                        pass
        missing = [v for v in values if v not in found]
        return not missing, "all typed in as numbers" if not missing else f"not found as numbers: {missing}"
    return Check(f"the {len(values)} given figures are typed in as numbers", fn)


def x_eval_all(book: SheetBook) -> tuple[dict[tuple[str, str], Any], list[str]]:
    from obench.formula import FormulaError, Unsupported

    ev = book.evaluator()
    out, problems = {}, []
    for s, ref, f in book.formula_cells():
        try:
            out[(s, ref)] = ev.cell(s, ref)
        except Unsupported as exc:
            problems.append(f"{s}!{ref} {f}: cannot evaluate ({exc})")
        except FormulaError as exc:
            out[(s, ref)] = f"#ERR {exc}"
            problems.append(f"{s}!{ref} {f}: {exc}")
    return out, problems


def x_formula_value(value: float, label: str, tol: float = 0.01) -> Check:
    def fn(book: SheetBook, ctx):
        vals, problems = x_eval_all(book)
        hits = [f"{s}!{r}" for (s, r), v in vals.items() if isinstance(v, (int, float)) and abs(v - value) <= tol]
        if hits:
            return True, f"{hits[0]} = {book.raw(*hits[0].split('!'))}"
        typed = [f"{s}!{r}" for s in book.sheets for r, c in book.cells[s].items()
                 if isinstance(c.raw, (int, float)) and abs(c.raw - value) <= tol]
        why = f"typed in as a number at {typed[0]}, not a formula" if typed else "no formula gives it"
        return (None if problems and not typed else False), f"{why}" + (f"; {problems[:2]}" if problems else "")
    return Check(f"a formula computes {label} = {value:g}", fn, ("total", "formula", label.split()[0]))


def x_cached_consistent() -> Check:
    def fn(book: SheetBook, ctx):
        vals, problems = x_eval_all(book)
        stored = [(s, r) for s, r, _ in book.formula_cells() if book.cells[s][r].cached is not None]
        if not book.formula_cells():
            return False, "no formulas at all"
        if not stored:
            return None, f"the file stores no computed values for its {len(book.formula_cells())} formulas"
        bad = []
        for s, r in stored:
            cached = book.cells[s][r].cached
            mine = vals.get((s, r))
            if isinstance(mine, (int, float)) and isinstance(cached, (int, float)) and abs(mine - cached) > 0.01:
                bad.append(f"{s}!{r} stores {cached}, computes {mine}")
            elif isinstance(cached, str) and cached.startswith("#"):
                bad.append(f"{s}!{r} stores {cached}")
        return not bad, f"{len(stored)} stored values match" if not bad else f"{bad[:4]}"
    return Check("stored values match what the formulas compute", fn)


def x_format(values: list[float], pattern: str, label: str, share: float = 0.8, suspect: str | None = None) -> Check:
    """The cells holding *values* (typed or computed) have a number format matching *pattern*. Formats matching
    *suspect* (a code the editor stored that Excel may not read the same way) make the check unknown, not passed."""
    def fn(book: SheetBook, ctx):
        vals, _ = x_eval_all(book)
        cells = []
        for s in book.sheets:
            for r, c in book.cells[s].items():
                v = vals.get((s, r), c.raw)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and any(abs(v - w) < 0.01 for w in values):
                    cells.append(c)
        if not cells:
            return False, "none of the amounts found"
        good = [c for c in cells if re.search(pattern, c.number_format or "", re.IGNORECASE)]
        formats = sorted({c.number_format for c in cells})
        if len(good) >= share * len(cells):
            return True, f"{len(good)}/{len(cells)} amount cells; formats {formats[:4]}"
        odd = [c for c in cells if suspect and re.search(suspect, c.number_format or "")]
        if odd and len(good) + len(odd) >= share * len(cells):
            return None, f"non-standard format code {odd[0].number_format!r} on {len(odd)}/{len(cells)} cells: whether Excel shows {label} is unverified"
        return False, f"{len(good)}/{len(cells)} amount cells; formats {formats[:4]}"
    return Check(f"amounts formatted as {label}", fn, (label,))


def x_chart(types: set[str] | None = None, title: str | None = None, sheet: str | None = None,
            not_over: str | None = None) -> Check:
    def fn(book: SheetBook, ctx):
        charts = [c for c in book.charts if sheet is None or c.sheet.lower() == sheet.lower()]
        if not charts:
            return False, "no chart"
        desc = [f"{c.sheet}: {c.types} {c.title!r} at {c.anchor}" for c in charts]
        for c in charts:
            if types and not set(c.types) & types:
                continue
            if title and norm(title) not in norm(c.title):
                continue
            if not c.refs:
                continue
            return True, desc[0] if len(desc) == 1 else str(desc)
        return False, f"charts: {desc}"
    what = "/".join(sorted(types)) if types else "a"
    extra = f" titled {title!r}" if title else ""
    return Check(f"{what} chart{extra} from the data", fn, ("chart",))


def x_sheets(names: list[str]) -> Check:
    def fn(book: SheetBook, ctx):
        have = [s.lower() for s in book.sheets]
        missing = [n for n in names if n.lower() not in have]
        return not missing, f"sheets {book.sheets}"
    return Check(f"sheets {names}", fn, ("sheet",))


def x_keeps(sheet: str, refs: list[str] | None = None, name: str = "") -> Check:
    """The original's cells on *sheet* (all, or *refs*) hold the same content."""
    def fn(book: SheetBook, ctx):
        if ctx.original is None:
            return None, "no original"
        orig = ctx.original.cells.get(sheet, {})
        targets = refs or list(orig)
        have = book.sheet_named(sheet)
        if have is None:
            return False, f"sheet {sheet} gone"
        diffs = []
        for r in targets:
            a = orig.get(r).raw if orig.get(r) else None
            b = book.raw(have, r)
            if str(a).replace(" ", "") != str(b).replace(" ", ""):
                diffs.append(f"{r}: {a!r} -> {b!r}")
        return not diffs, "unchanged" if not diffs else f"changed: {diffs[:5]}"
    return Check(name or f"{sheet} cells kept", fn)


# --------------------------------------------------------------------------- Deck


def p_slides(lo: int, hi: int) -> Check:
    def fn(deck: Deck, ctx):
        n = len(deck.slides)
        return lo <= n <= hi, f"{n} slides: {[s.title[:40] for s in deck.slides]}"
    label = f"{lo} slides" if lo == hi else f"{lo}-{hi} slides"
    return Check(label, fn, ("slide",))


def p_no_overflow() -> Check:
    def fn(deck: Deck, ctx):
        bad = [f"slide {i}: {s.name} ({s.overflow}) {s.text[:40]!r}" for i, sl in enumerate(deck.slides, 1)
               for s in sl.shapes if s.overflow]
        return not bad, "no text box looks overfull" if not bad else f"{bad[:4]}"
    return Check("no text overflows its box (estimated from geometry)", fn)


def p_in_bounds(tol: float = 2.0) -> Check:
    def fn(deck: Deck, ctx):
        bad = []
        for i, sl in enumerate(deck.slides, 1):
            for s in sl.shapes:
                if s.x < -tol or s.y < -tol or s.x + s.w > deck.width + tol or s.y + s.h > deck.height + tol:
                    bad.append(f"slide {i}: {s.name} at ({s.x:.0f},{s.y:.0f}) {s.w:.0f}x{s.h:.0f}")
        return not bad, "every shape on its slide" if not bad else f"{bad[:4]}"
    return Check("every shape inside the slide", fn)


def p_every_slide_has_text() -> Check:
    def fn(deck: Deck, ctx):
        empty = [i for i, s in enumerate(deck.slides, 1) if not s.text().strip()]
        return not empty, "all slides carry text" if not empty else f"empty slides: {empty}"
    return Check("no empty slide", fn)


def p_sig(slide) -> list[tuple]:
    out = []
    for s in slide.shapes:
        sizes = tuple(round(sz or 0) for _, sz in s.paragraphs)
        out.append((s.kind, norm(s.text), tuple(tuple(norm(c) for c in r) for r in s.table or []),
                    round(s.x), round(s.y), round(s.w), round(s.h), sizes))
    return out


def same_slide(a, b, geometry_tol: float = 2.0) -> tuple[bool, str]:
    sa, sb = p_sig(a), p_sig(b)
    if len(sa) != len(sb):
        return False, f"{len(sa)} shapes -> {len(sb)}"
    for x, y in zip(sa, sb):
        if x[:3] != y[:3]:
            return False, f"shape text {x[1][:40]!r} -> {y[1][:40]!r}"
        if any(abs(p - q) > geometry_tol for p, q in zip(x[3:7], y[3:7])):
            return False, f"{x[1][:30]!r} moved/resized {x[3:7]} -> {y[3:7]}"
        if x[7] != y[7]:
            return False, f"{x[1][:30]!r} font sizes {x[7]} -> {y[7]}"
    if norm(a.notes) != norm(b.notes):
        return False, f"notes {a.notes!r} -> {b.notes!r}"
    return True, "unchanged"


def p_slide_kept(orig_index: int, new_index: int | None = None, name: str = "") -> Check:
    def fn(deck: Deck, ctx):
        if ctx.original is None:
            return None, "no original"
        ni = orig_index if new_index is None else new_index
        if ni > len(deck.slides):
            return False, f"no slide {ni}"
        return same_slide(ctx.original.slides[orig_index - 1], deck.slides[ni - 1])
    return Check(name or f"slide {orig_index} unchanged", fn)


# --------------------------------------------------------------------------- PDF


def f_pages(n: int) -> Check:
    def fn(pdf: PdfDoc, ctx):
        return len(pdf.pages) == n, f"{len(pdf.pages)} pages"
    return Check(f"{n} pages", fn)


def f_text_keeps(phrases: list[str]) -> Check:
    def fn(pdf: PdfDoc, ctx):
        missing = [p for p in phrases if norm(p) not in norm(pdf.text())]
        return not missing, "kept" if not missing else f"lost: {missing}"
    return Check("the rest of the text is kept", fn)


def f_fields_kept(names: list[str]) -> Check:
    def fn(pdf: PdfDoc, ctx):
        missing = [n for n in names if n not in pdf.fields]
        return not missing, f"fields {sorted(pdf.fields)}"
    return Check(f"form fields {names} still there", fn)
