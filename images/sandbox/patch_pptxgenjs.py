#!/usr/bin/env python3
"""Make pptxgenjs write a layout's title placeholder the way PowerPoint's own files do.

pptxgenjs numbers every placeholder of a layout ``100 + its position``, the title too:
``<p:ph idx="100" type="title"/>``.  In PresentationML a title placeholder has no ``idx``
(the default, 0), and readers rely on it: python-pptx's ``slide.shapes.title`` looks for
idx 0, so a deck with a real title on every slide reads as having none.

The library writes a slide's placeholder from its layout's object, so the one change here
keeps the two the same.  Only the first title of a layout gets the default: two placeholders
of one layout must not share an idx.  This changes what pptxgenjs generates and nothing else:
it never opens an existing deck.

usage: patch_pptxgenjs.py <pptxgenjs package directory>
Fails, naming the file, when the library's line is not there, so a new pptxgenjs cannot lose
the change without anyone noticing.
"""
from __future__ import annotations

import sys
from pathlib import Path

ORIGINAL = "object[key].options._placeholderIdx = 100 + idx;"
PATCHED = (
    "object[key].options._placeholderIdx = object[key].options._placeholderType === 'title'"
    " && !tgt._slideObjects.some((o) => o.options && o.options._placeholderType === 'title')"
    " ? 0 : 100 + idx;"
)
# The Node builds: require() loads the first, `import` and bundlers the second. The browser bundles never run in a sandbox.
BUILDS = ("dist/pptxgen.cjs.js", "dist/pptxgen.es.js")


def patch_text(text: bytes, name: str) -> bytes:
    """*text* with the line changed; already changed is fine, anything else is an error."""
    original, patched = ORIGINAL.encode(), PATCHED.encode()
    if text.count(original) == 1 and patched not in text:
        return text.replace(original, patched)
    if text.count(patched) == 1 and original not in text:
        return text
    raise SystemExit(f"{name}: expected the placeholder index line exactly once; update {Path(__file__).name}")


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
