"""The sandbox image's change to pptxgenjs: a layout's title placeholder has no idx."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "images" / "sandbox" / "patch_pptxgenjs.py"
_spec = importlib.util.spec_from_file_location("patch_pptxgenjs", SCRIPT)
patch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(patch)


def _package(tmp_path: Path, line: str = patch.ORIGINAL) -> Path:
    dist = tmp_path / "pptxgenjs" / "dist"
    dist.mkdir(parents=True)
    for build in ("pptxgen.cjs.js", "pptxgen.es.js"):
        (dist / build).write_text(f"before\n    {line}\nafter\n", encoding="utf-8")
    return dist.parent


def test_both_builds_are_changed_and_nothing_else(tmp_path):
    package = _package(tmp_path)
    assert patch.main(["patch", str(package)]) == 0
    for build in patch.BUILDS:
        assert (package / build).read_text(encoding="utf-8") == f"before\n    {patch.PATCHED}\nafter\n"


def test_running_it_twice_changes_nothing_more(tmp_path):
    package = _package(tmp_path)
    patch.main(["patch", str(package)])
    first = [(package / b).read_bytes() for b in patch.BUILDS]
    patch.main(["patch", str(package)])
    assert [(package / b).read_bytes() for b in patch.BUILDS] == first


def test_a_library_without_the_line_stops_the_build_and_is_left_alone(tmp_path):
    package = _package(tmp_path)
    es = package / "dist" / "pptxgen.es.js"
    es.write_text("the library changed\n", encoding="utf-8")
    cjs_before = (package / "dist" / "pptxgen.cjs.js").read_bytes()
    with pytest.raises(SystemExit, match="pptxgen.es.js"):
        patch.main(["patch", str(package)])
    assert (package / "dist" / "pptxgen.cjs.js").read_bytes() == cjs_before


def test_the_image_applies_it_in_the_sandbox_stage_only():
    """The tools stage is hashed into the desktop guest's key (images/guest/inputs.sh)."""
    text = (ROOT / "images" / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
    before, after = text.split("FROM tools AS sandbox\n", 1)
    assert "patch_pptxgenjs" not in before
    assert "patch_pptxgenjs.py /usr/lib/node_modules/pptxgenjs" in after


DECK = r"""
const pptxgen = require("pptxgenjs");
(async () => {
  const pres = new pptxgen();
  const ph = (name, type, y) => ({ placeholder: { options: { name, type, x: 1, y, w: 8, h: 1 }, text: "" } });
  pres.defineSlideMaster({ title: "ONE", objects: [ph("title", "title", 0.5), ph("body", "body", 2)] });
  pres.defineSlideMaster({ title: "BODY_FIRST", objects: [ph("body", "body", 2), ph("title", "title", 0.5)] });
  pres.defineSlideMaster({ title: "TWO_TITLES", objects: [ph("title", "title", 0.5), ph("title2", "title", 2)] });
  for (const m of ["ONE", "BODY_FIRST"]) {
    const s = pres.addSlide({ masterName: m });
    s.addText("Title of " + m, { placeholder: "title" });
    s.addText("Body of " + m, { placeholder: "body" });
  }
  const t = pres.addSlide({ masterName: "TWO_TITLES" });
  t.addText("First", { placeholder: "title" });
  t.addText("Second", { placeholder: "title2" });
  await pres.writeFile({ fileName: process.argv[2] });
})();
"""

READ = r"""
import json, sys
from pptx import Presentation
deck = Presentation(sys.argv[1])
out = []
for slide in deck.slides:
    title = slide.shapes.title
    out.append({
        "title": title.text if title is not None else None,
        "idx": sorted(p.placeholder_format.idx for p in slide.placeholders),
        "layout_idx": sorted(p.placeholder_format.idx for p in slide.slide_layout.placeholders),
        "inherited": [p._base_placeholder is not None for p in slide.placeholders],
    })
print(json.dumps(out))
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_a_generated_deck_has_titles_python_pptx_finds(tmp_path):
    """Against the real library, where it is installed (the sandbox image)."""
    pytest.importorskip("pptx")
    root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
    source = Path(root) / "pptxgenjs"
    if not source.is_dir():
        pytest.skip("needs pptxgenjs")
    modules = tmp_path / "node_modules"
    shutil.copytree(source, modules / "pptxgenjs", symlinks=True)
    text = (modules / "pptxgenjs" / "dist" / "pptxgen.cjs.js").read_text(encoding="utf-8")
    if patch.ORIGINAL in text:
        patch.main(["patch", str(modules / "pptxgenjs")])
    (tmp_path / "deck.js").write_text(DECK, encoding="utf-8")
    subprocess.run(["node", "deck.js", "deck.pptx"], cwd=tmp_path, check=True)
    (tmp_path / "read.py").write_text(READ, encoding="utf-8")
    import sys

    slides = json.loads(subprocess.run(
        [sys.executable, "read.py", "deck.pptx"], cwd=tmp_path, check=True, capture_output=True, text=True,
    ).stdout)
    assert [s["title"] for s in slides] == ["Title of ONE", "Title of BODY_FIRST", "First"]
    for slide in slides:
        assert slide["idx"] == slide["layout_idx"] and slide["idx"][0] == 0
        assert all(slide["inherited"])
    # A second title of one layout keeps a number of its own.
    assert len(set(slides[2]["idx"])) == 2
