"""The document server's own render of a deck, and text overflow measured on it.

``render_pdf`` converts a document to PDF with the document server's converter, x2t, in a shell opened by the command
prefix in ``OFFICEBENCH_RENDER_SHELL`` (``docker run -i --rm <document server image>``, ``kubectl exec -i <converter
pod> --``). Unset, or when the converter fails, there is no PDF and the reason says why: overflow is then unknown,
never estimated.

``measure_overflow`` reads the PDF's text, one page per slide, and gives each rendered line to the top-level text box
it belongs to. A box overflows when one of its lines has its baseline more than ``TOLERANCE_PT`` below the box, or
ends more than that right of it. Descenders below the box are not an overflow a reader sees.
"""
from __future__ import annotations

import io
import os
import re
import shlex
import subprocess
import unicodedata
from dataclasses import dataclass, replace
from typing import Any

from obench.docs import Deck, Shape, norm

RENDER_SHELL_ENV = "OFFICEBENCH_RENDER_SHELL"
X2T_ENV = "OFFICEBENCH_X2T"
DEFAULT_X2T = "/var/www/euro-office/documentserver/server/FileConverter/bin/x2t"
TIMEOUT_S = 120.0
TOLERANCE_PT = 2.0

# Runs in the renderer's shell with x2t's path as $1 and the document's type as $2: the document comes on stdin, the
# PDF goes to stdout and everything x2t says to stderr. x2t runs from its own directory; 513 is its PDF format.
SCRIPT = r"""set -e
X2T="$1"
D="$(mktemp -d)"
trap 'rm -rf "$D"' EXIT
cat > "$D/in.$2"
cat > "$D/params.xml" <<XML
<?xml version="1.0" encoding="utf-8"?><TaskQueueDataConvert xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema"><m_sFileFrom>$D/in.$2</m_sFileFrom><m_sFileTo>$D/out.pdf</m_sFileTo><m_nFormatTo>513</m_nFormatTo><m_bIsNoBase64>true</m_bIsNoBase64></TaskQueueDataConvert>
XML
cd "$(dirname "$X2T")"
"$X2T" "$D/params.xml" >&2
cat "$D/out.pdf"
"""


@dataclass
class RenderResult:
    pdf: bytes | None
    reason: str  # why there is no PDF; empty when there is one


def _tail(stderr: bytes) -> str:
    text = stderr.decode("utf-8", "replace").strip()
    return text[-300:] if text else "nothing on stderr"


def render_pdf(document: bytes, file_type: str, timeout: float = TIMEOUT_S) -> RenderResult:
    """The document server's PDF of *document* (a ``file_type`` file such as ``pptx``), or why there is none."""
    if not re.fullmatch(r"[a-z0-9]+", file_type):
        raise ValueError(f"not a file type: {file_type!r}")
    prefix = os.environ.get(RENDER_SHELL_ENV, "").strip()
    if not prefix:
        return RenderResult(None, f"{RENDER_SHELL_ENV} is not set: nothing renders the document")
    x2t = os.environ.get(X2T_ENV, "").strip() or DEFAULT_X2T
    argv = shlex.split(prefix) + ["sh", "-c", SCRIPT, "render", x2t, file_type]
    try:
        proc = subprocess.run(argv, input=document, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return RenderResult(None, f"the renderer timed out after {timeout:g} s")
    except OSError as exc:
        return RenderResult(None, f"the renderer did not start: {exc}")
    if proc.returncode != 0:
        return RenderResult(None, f"the renderer exited with status {proc.returncode}: {_tail(proc.stderr)}")
    if not proc.stdout:
        return RenderResult(None, f"the renderer's output was empty: {_tail(proc.stderr)}")
    if not proc.stdout.startswith(b"%PDF"):
        return RenderResult(None, f"the renderer's output is not a PDF: {proc.stdout[:40]!r}")
    return RenderResult(proc.stdout, "")


# --------------------------------------------------------------------------- reading the render


@dataclass(frozen=True)
class Line:
    """Text one show operation put on a slide, in points from the slide's top-left corner.

    A line is ``1.2`` times its font size tall and its bottom is its baseline: descenders are not counted.
    """

    text: str
    x0: float
    top: float
    x1: float  # where its last visible glyph ends
    bottom: float


def _resolve(obj: Any) -> Any:
    return obj.get_object() if hasattr(obj, "get_object") else obj


def _hex(token: bytes) -> bytes:
    digits = re.sub(rb"\s", b"", token)
    return bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode())


def _to_unicode(cmap: bytes) -> dict[int, str]:
    """A ToUnicode CMap's character codes and the text they stand for."""
    out: dict[int, str] = {}
    hex_token = rb"<([0-9A-Fa-f\s]*)>"
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", cmap, re.S):
        tokens = re.findall(hex_token, block)
        for src, dst in zip(tokens[0::2], tokens[1::2]):
            out[int.from_bytes(_hex(src), "big")] = _hex(dst).decode("utf-16-be", "replace")
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", cmap, re.S):
        for lo, hi, dst in re.findall(hex_token + rb"\s*" + hex_token + rb"\s*(<[0-9A-Fa-f\s]*>|\[[^\]]*\])", block):
            first, last = int.from_bytes(_hex(lo), "big"), int.from_bytes(_hex(hi), "big")
            codes = range(first, min(last, first + 0xFFFF) + 1)
            if dst.startswith(b"["):
                for code, item in zip(codes, re.findall(hex_token, dst)):
                    out[code] = _hex(item).decode("utf-16-be", "replace")
                continue
            base = _hex(dst[1:-1])
            for code in codes:
                value = int.from_bytes(base, "big") + code - first
                if not base or value >= 256 ** len(base):
                    break
                out[code] = value.to_bytes(len(base), "big").decode("utf-16-be", "replace")
    return out


def _cid_widths(entries: Any) -> dict[int, float]:
    """A CID font's ``/W``: ``c [w1 w2 ...]`` and ``c_first c_last w`` entries."""
    items = [_resolve(x) for x in entries or []]
    out: dict[int, float] = {}
    i = 0
    while i + 1 < len(items):
        first, nxt = int(items[i]), items[i + 1]
        if isinstance(nxt, list):
            for k, width in enumerate(nxt):
                out[first + k] = float(_resolve(width))
            i += 2
        elif i + 2 < len(items):
            for code in range(first, min(int(nxt), first + 0xFFFF) + 1):
                out[code] = float(items[i + 2])
            i += 3
        else:
            break
    return out


@dataclass
class _TextState:
    font: _Font | None = None
    size: float = 0.0
    char_spacing: float = 0.0
    word_spacing: float = 0.0
    scale: float = 1.0


@dataclass
class _Font:
    two_byte: bool
    widths: dict[int, float]  # character code -> advance per point of font size
    default_width: float
    unicode: dict[int, str]

    def show(self, items: list[Any], st: _TextState) -> tuple[str, float, float]:
        """A Tj string or TJ array: its text, its advance, and where its last visible glyph ends."""
        text: list[str] = []
        advance = ink = 0.0
        for item in items:
            item = _resolve(item)
            if isinstance(item, (int, float)):
                advance -= float(item) / 1000 * st.size * st.scale
                continue
            raw = item.get_original_bytes() if hasattr(item, "get_original_bytes") else bytes(item)
            if self.two_byte:
                codes = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw) - 1, 2)]
            else:
                codes = list(raw)
            for code in codes:
                char = self.unicode.get(code, "" if self.two_byte else chr(code))
                width = self.widths.get(code, self.default_width) * st.size + st.char_spacing
                if not self.two_byte and code == 32:
                    width += st.word_spacing
                advance += width * st.scale
                text.append(char)
                if char.strip():
                    ink = advance
        return "".join(text), advance, ink


def _font(font: Any) -> _Font:
    font = _resolve(font)
    unicode = _to_unicode(_resolve(font["/ToUnicode"]).get_data()) if "/ToUnicode" in font else {}
    if font.get("/Subtype") == "/Type0":
        descendant = _resolve(_resolve(font["/DescendantFonts"])[0])
        widths = _cid_widths(descendant["/W"] if "/W" in descendant else [])
        default = float(descendant["/DW"]) if "/DW" in descendant else 1000.0
        return _Font(True, {c: w / 1000 for c, w in widths.items()}, default / 1000, unicode)
    # A simple font: one byte a character; a Type3 font scales its widths by its own matrix.
    scale = float(_resolve(font["/FontMatrix"])[0]) if "/FontMatrix" in font else 0.001
    first = int(font["/FirstChar"]) if "/FirstChar" in font else 0
    widths = {first + i: float(_resolve(w)) * scale for i, w in enumerate(font["/Widths"] if "/Widths" in font else [])}
    descriptor = _resolve(font["/FontDescriptor"]) if "/FontDescriptor" in font else {}
    missing = float(descriptor["/MissingWidth"]) if "/MissingWidth" in descriptor else 0.0
    return _Font(False, widths, missing * scale, unicode)


def _page_fonts(page: Any) -> dict[str, _Font]:
    """The page's fonts by resource name, then those of its forms where a name is not taken."""
    fonts: dict[str, _Font] = {}

    def collect(resources: Any, depth: int) -> None:
        resources = _resolve(resources)
        if not resources or depth > 4:
            return
        for name, ref in (_resolve(resources["/Font"]) if "/Font" in resources else {}).items():
            if name not in fonts:
                try:
                    fonts[name] = _font(ref)
                except Exception:  # noqa: BLE001 - a font this cannot read: its text is not measured
                    pass
        for ref in (_resolve(resources["/XObject"]) if "/XObject" in resources else {}).values():
            xobject = _resolve(ref)
            if xobject.get("/Subtype") == "/Form" and "/Resources" in xobject:
                collect(xobject["/Resources"], depth + 1)

    collect(page["/Resources"] if "/Resources" in page else None, 0)
    return fonts


def _page_lines(page: Any, width: float, height: float) -> list[Line]:
    box = page.mediabox
    left, top = float(box.left), float(box.top)
    sx = width / float(box.width) if width > 0 and float(box.width) > 0 else 1.0
    sy = height / float(box.height) if height > 0 and float(box.height) > 0 else 1.0
    fonts = _page_fonts(page)
    lines: list[Line] = []
    state = _TextState()
    saved: list[_TextState] = []
    moved = 0.0  # how far the text shown since the line matrix last moved has gone (pypdf's tm does not advance)

    def visit(op: bytes, operands: list[Any], cm: list[float], tm: list[float]) -> None:
        nonlocal state, moved
        if op == b"q":
            saved.append(replace(state))
        elif op == b"Q":
            state = saved.pop() if saved else state
        elif op in (b"BT", b"Td", b"TD", b"Tm", b"T*", b"'", b'"'):
            moved = 0.0
        elif op == b"Tf" and len(operands) == 2:
            state.font, state.size = fonts.get(operands[0]), float(operands[1])
        elif op == b"Tc" and operands:
            state.char_spacing = float(operands[0])
        elif op == b"Tw" and operands:
            state.word_spacing = float(operands[0])
        elif op == b"Tz" and operands:
            state.scale = float(operands[0]) / 100
        elif op in (b"Tj", b"TJ") and operands and state.font is not None:
            items = list(_resolve(operands[0])) if op == b"TJ" else [operands[0]]
            text, advance, ink = state.font.show(items, state)
            start, moved = moved, moved + advance
            # Text space to the page: the text matrix, then the current transformation matrix.
            a, b = tm[0] * cm[0] + tm[1] * cm[2], tm[0] * cm[1] + tm[1] * cm[3]
            c, d = tm[2] * cm[0] + tm[3] * cm[2], tm[2] * cm[1] + tm[3] * cm[3]
            e, f = tm[4] * cm[0] + tm[5] * cm[2] + cm[4], tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            if not text.strip() or abs(b) > 1e-3 or abs(c) > 1e-3 or a <= 0 or d <= 0:
                return  # nothing visible, or rotated or mirrored text: no box edge to compare it with
            size = state.size * d * sy
            baseline = (top - f) * sy
            lines.append(Line(text, (e + start * a - left) * sx, baseline - 1.2 * size,
                              (e + (start + ink) * a - left) * sx, baseline))

    page.extract_text(visitor_operand_before=visit)
    return lines


def rendered_lines(pdf: bytes, width: float, height: float) -> list[list[Line]]:
    """Each page's lines, a page scaled to a *width* x *height* slide."""
    from pypdf import PdfReader

    return [_page_lines(page, width, height) for page in PdfReader(io.BytesIO(pdf)).pages]


# --------------------------------------------------------------------------- measuring


# A list's bullet or number, which the render draws and the paragraph's text does not hold.
_LIST_MARKER = re.compile(r"^(?:[^\w]+|\(?(?:\d{1,3}|[a-z]|[ivxlcdm]{1,6})[.)])")


def _squash(text: str) -> str:
    """Compared without any whitespace: the render shows a word gap as a move, not always as a space."""
    return re.sub(r"\s+", "", norm(unicodedata.normalize("NFKC", text or "")))


def _holds(shape: Shape) -> str:
    if shape.kind == "table":
        return _squash(" ".join(" ".join(row) for row in shape.table or []))
    return _squash(shape.text)


def _owner(line: Line, shapes: list[tuple[Shape, str]], others: list[Shape]) -> Shape | None:
    """The shape of *shapes* (with the text each holds) whose text *line* is.

    A shape holds the line when its text contains the line's (with or without a leading list marker) and its box spans
    the line's start or middle. The holder whose box the line is in wins. A line out of every holder's box but in the
    box of one of *others* is that shape's own text (a chart's labels, a list number, a field), so nobody's here.
    Else the nearest holder: its text ran out of its box.
    """
    text = _squash(line.text)
    texts = {t for t in (text, _LIST_MARKER.sub("", text)) if t}
    if not texts:
        return None
    middle = (line.top + line.bottom) / 2

    def across(s: Shape) -> bool:
        return any(s.x - TOLERANCE_PT <= x <= s.x + s.w + TOLERANCE_PT for x in (line.x0, (line.x0 + line.x1) / 2))

    def inside(s: Shape) -> bool:
        return across(s) and s.y <= middle <= s.y + s.h

    holders = [s for s, held in shapes if any(t in held for t in texts) and across(s)]
    if not holders:
        return None
    within = [s for s in holders if inside(s)]
    if within:
        return within[-1]  # drawn last, so on top
    if any(inside(s) for s in others if all(s is not h for h in holders)):
        return None
    return min(holders, key=lambda s: max(0.0, s.y - line.bottom, line.top - (s.y + s.h)))


def _overflow(shape: Shape, lines: list[Line]) -> str:
    past_bottom = max(line.bottom for line in lines) - (shape.y + shape.h)
    past_right = max(line.x1 for line in lines) - (shape.x + shape.w)
    if past_bottom > TOLERANCE_PT:
        return f"text ends {past_bottom:.0f} pt past the box's bottom"
    if past_right > TOLERANCE_PT:
        return f"text ends {past_right:.0f} pt past the box's right side"
    return ""


def measure_lines(pages: list[list[Line]], deck: Deck) -> None:
    """Set ``overflow`` on each top-level text box whose lines are found, page *i* being slide *i*."""
    if len(pages) != len(deck.slides):
        n, m = len(pages), len(deck.slides)
        deck.render_note = f"the render has {n} page{'s' * (n != 1)} for {m} slide{'s' * (m != 1)}"
        return
    measured = 0
    for slide, lines in zip(deck.slides, pages):
        # Group children are not measured: their offsets are in the group's own space, not the slide's.
        top_level = [s for s in slide.shapes if not s.in_group]
        shapes = [(s, held) for s in top_level if s.kind in ("text", "table") and (held := _holds(s))]
        # What draws text of its own; a picture or an empty box (a card under a text box) does not.
        others = [s for s in top_level if s.kind in ("chart", "other")] + [s for s, _ in shapes]
        found: dict[int, list[Line]] = {}
        for line in lines:
            owner = _owner(line, shapes, others)
            if owner is not None and owner.kind == "text":
                found.setdefault(id(owner), []).append(line)
        for shape, _ in shapes:
            if id(shape) in found:
                shape.overflow = _overflow(shape, found[id(shape)])
                measured += 1
    if not measured:
        deck.render_note = "no text box's text was found in the render"


def measure_overflow(pdf: bytes, deck: Deck) -> None:
    """Set ``overflow`` on each text box of *deck* measured on its render *pdf*: ``""`` fits, else how far it runs."""
    try:
        pages = rendered_lines(pdf, deck.width, deck.height)
    except Exception as exc:  # noqa: BLE001 - a render pypdf cannot read leaves overflow unknown
        deck.render_note = f"the render could not be read: {type(exc).__name__}: {exc}"
        return
    measure_lines(pages, deck)
