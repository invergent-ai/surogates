#!/usr/bin/env python3
"""Make pptxgenjs write the chart series XML the schema and common readers expect.

Three changes to what the library generates for bar, line, area and radar charts:

* ``c:invertIfNegative`` is written for bar series only.  ISO 29500's ``CT_LineSer``,
  ``CT_AreaSer`` and ``CT_RadarSer`` have no such element.
* A line series' ``c:marker`` comes before its ``c:dLbls``, the order ``CT_LineSer`` requires.
  The marker itself (symbol, size, fill, outline) is the same.
* One level of category labels is a plain ``c:strRef``.  ``c:multiLvlStrRef`` is valid, but it is
  for hierarchical labels, and a reader that handles flat categories only finds none in it.
  Two or more levels stay as they are.

This changes what pptxgenjs generates and nothing else: it never opens an existing deck.

usage: patch_pptxgenjs_charts.py <pptxgenjs package directory>
Fails, naming the file, when the library's code is not there, so a new pptxgenjs cannot lose
the change without anyone noticing.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

INVERT = b"""strXml += '  <c:invertIfNegative val="0"/>';"""
INVERT_BAR = (
    b"if (chartType === CHART_TYPE.BAR || chartType === CHART_TYPE.BAR3D)"
    b""" strXml += '  <c:invertIfNegative val="0"/>';"""
)
SERIES = re.compile(
    re.escape(INVERT)
    + rb"(?P<labels>(?:(?!<c:marker>).)*?'<c:dLbls>'(?:(?!<c:marker>).)*?)"
    + rb"(?P<marker>// 'c:marker' tag: `lineDataSymbol`.*?strXml \+= '</c:marker>';\s*\})"
    + rb"(?P<next>\s*// Allow users with a single data set)",
    re.S,
)
CATEGORIES = re.compile(rb"else \{(?P<indent>\r?\n[ \t]*)strXml \+= '  <c:multiLvlStrRef>';")
ONE_LEVEL = b"else if (obj.labels.length === 1) {"
PLAIN_CATEGORIES = (
    b"strXml += '  <c:strRef>';",
    b"strXml += `    <c:f>Sheet1!$A$2:$A$${obj.labels[0].length + 1}</c:f>`;",
    b"strXml += '    <c:strCache>';",
    b"""strXml += `      <c:ptCount val="${obj.labels[0].length}"/>`;""",
    b"""obj.labels[0].forEach((label, idx) => (strXml += `<c:pt idx="${idx}"><c:v>${encodeXmlEntities(label)}</c:v></c:pt>`));""",
    b"strXml += '    </c:strCache>';",
    b"strXml += '  </c:strRef>';",
)
# The Node builds: require() loads the first, `import` and bundlers the second. The browser bundles never run in a sandbox.
BUILDS = ("dist/pptxgen.cjs.js", "dist/pptxgen.es.js")


def _series(match: re.Match[bytes]) -> bytes:
    labels = match["labels"]
    indent = labels[: len(labels) - len(labels.lstrip())]
    return INVERT_BAR + indent + match["marker"] + labels.rstrip() + match["next"]


def _categories(match: re.Match[bytes]) -> bytes:
    indent = match["indent"]
    plain = b"".join(indent + b"    " + line for line in PLAIN_CATEGORIES)
    return ONE_LEVEL + plain + indent + b"} else {" + indent + b"strXml += '  <c:multiLvlStrRef>';"


def patch_text(text: bytes, name: str) -> bytes:
    """*text* with the three changes; already changed is fine, anything else is an error."""
    if text.count(INVERT_BAR) == 1 and text.count(ONE_LEVEL) == 1 and text.count(INVERT) == 1:
        return text
    if text.count(INVERT) != 1 or INVERT_BAR in text or ONE_LEVEL in text:
        raise SystemExit(f"{name}: expected the series line exactly once; update {Path(__file__).name}")
    text, series = SERIES.subn(_series, text)
    text, categories = CATEGORIES.subn(_categories, text)
    if series != 1 or categories != 1:
        raise SystemExit(f"{name}: expected the chart series code exactly once; update {Path(__file__).name}")
    return text


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit(__doc__)
    package = Path(argv[1])
    # Both are checked before either is written: never half a patched library.
    changed = {}
    for build in BUILDS:
        path = package / build
        changed[path] = patch_text(path.read_bytes(), str(path))
    for path, text in changed.items():
        path.write_bytes(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
