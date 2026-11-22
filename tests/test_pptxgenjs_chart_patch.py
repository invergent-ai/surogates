"""The sandbox image's change to pptxgenjs's chart series: schema order and plain one-level categories."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "images" / "sandbox" / "patch_pptxgenjs_charts.py"
SCHEMA = ROOT / "skills/productivity/pptx/scripts/office/schemas/ISO-IEC29500-4_2016/dml-chart.xsd"
_spec = importlib.util.spec_from_file_location("patch_pptxgenjs_charts", SCRIPT)
patch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(patch)

# The library's code around the three changes, as 4.0.1 has it.
LIBRARY = r"""before
                strXml += '  </c:spPr>';
                strXml += '  <c:invertIfNegative val="0"/>';
                // Data Labels per series
                if (chartType !== CHART_TYPE.RADAR) {
                    strXml += '<c:dLbls>';
                    strXml += '</c:dLbls>';
                }
                // 'c:marker' tag: `lineDataSymbol`
                if (chartType === CHART_TYPE.LINE || chartType === CHART_TYPE.RADAR) {
                    strXml += '<c:marker>';
                    strXml += '</c:marker>';
                }
                // Allow users with a single data set to pass their own array of colors (check for this using != ours)
                    else {
                        strXml += '  <c:multiLvlStrRef>';
                        strXml += '  </c:multiLvlStrRef>';
                    }
after
"""


def _package(tmp_path: Path, text: str = LIBRARY) -> Path:
    dist = tmp_path / "pptxgenjs" / "dist"
    dist.mkdir(parents=True)
    for build in ("pptxgen.cjs.js", "pptxgen.es.js"):
        (dist / build).write_bytes(text.encode())
    return dist.parent


def test_both_builds_get_the_three_changes(tmp_path):
    package = _package(tmp_path)
    assert patch.main(["patch", str(package)]) == 0
    for build in patch.BUILDS:
        text = (package / build).read_bytes()
        assert patch.INVERT_BAR in text
        assert text.index(b"'<c:marker>'") < text.index(b"'<c:dLbls>'")
        assert text.index(patch.ONE_LEVEL) < text.index(b"'  <c:multiLvlStrRef>'")
        assert text.startswith(b"before\n") and text.endswith(b"after\n")


def test_running_it_twice_changes_nothing_more(tmp_path):
    package = _package(tmp_path)
    patch.main(["patch", str(package)])
    first = [(package / b).read_bytes() for b in patch.BUILDS]
    patch.main(["patch", str(package)])
    assert [(package / b).read_bytes() for b in patch.BUILDS] == first


@pytest.mark.parametrize("missing", [
    "'  <c:invertIfNegative val=\"0\"/>'",
    "// 'c:marker' tag: `lineDataSymbol`",
    "'  <c:multiLvlStrRef>'",
])
def test_a_library_without_the_code_stops_the_build_and_is_left_alone(tmp_path, missing):
    package = _package(tmp_path)
    es = package / "dist" / "pptxgen.es.js"
    es.write_text(LIBRARY.replace(missing, "'changed'"), encoding="utf-8")
    cjs_before = (package / "dist" / "pptxgen.cjs.js").read_bytes()
    with pytest.raises(SystemExit, match="pptxgen.es.js"):
        patch.main(["patch", str(package)])
    assert (package / "dist" / "pptxgen.cjs.js").read_bytes() == cjs_before


def test_the_image_applies_it_in_the_sandbox_stage_only():
    """The tools stage is hashed into the desktop guest's key (images/guest/inputs.sh)."""
    text = (ROOT / "images" / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
    before, after = text.split("FROM tools AS sandbox\n", 1)
    assert "patch_pptxgenjs_charts" not in before
    assert "patch_pptxgenjs_charts.py /usr/lib/node_modules/pptxgenjs" in after


DECK = r"""
const pptxgen = require("pptxgenjs");
(async () => {
  const pres = new pptxgen();
  const labels = ["North & East", "South", "West"];
  const flat = [{ name: "Actual", labels, values: [12, 0, 7.5] }, { name: "Plan", labels, values: [10, 4, 8] }];
  const levels = [["Q1", "Q2", "Q3", "Q4"], ["H1", "", "H2", ""]];
  const multi = [{ name: "A", labels: levels, values: [1, 2, 3, 4] }];
  const box = { x: 1, y: 1, w: 8, h: 4 };
  pres.addSlide().addChart(pres.charts.LINE, flat, { ...box, lineDataSymbol: "diamond", lineDataSymbolSize: 9, showValue: true });
  pres.addSlide().addChart(pres.charts.BAR, flat, { ...box, barDir: "col" });
  pres.addSlide().addChart(pres.charts.AREA, flat, box);
  pres.addSlide().addChart(pres.charts.RADAR, flat, { ...box, lineDataSymbol: "square" });
  pres.addSlide().addChart(pres.charts.BAR, multi, { ...box, barDir: "col", catAxisMultiLevelLabels: true });
  pres.addSlide().addChart(pres.charts.LINE, flat, { ...box, lineDataSymbol: "none" });
  await pres.writeFile({ fileName: process.argv[2] });
})();
"""

READ = r"""
import json, sys
from pptx import Presentation
out = []
for slide in Presentation(sys.argv[1]).slides:
    chart = next(s.chart for s in slide.shapes if s.has_chart)
    plot = chart.plots[0]
    out.append({
        "type": str(chart.chart_type).split(" ")[0],
        "cats": [str(c) for c in plot.categories],
        "depth": plot.categories.depth,
        "series": [[s.name, list(s.values)] for s in plot.series],
        "xml": chart.part.blob.decode("utf-8"),
    })
print(json.dumps(out))
"""


def _library() -> Path:
    found = subprocess.run(["node", "-p", "require.resolve('pptxgenjs')"], capture_output=True, text=True)
    if found.returncode == 0:
        return Path(found.stdout.strip()).parents[1]  # <package>/dist/pptxgen.cjs.js
    npm = shutil.which("npm")  # require() does not search npm's global root
    root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True).stdout.strip() if npm else ""
    source = Path(root) / "pptxgenjs"
    if not root or not source.is_dir():
        pytest.skip("needs pptxgenjs")
    return source


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_a_generated_deck_keeps_every_chart_feature(tmp_path):
    """Against the real library: stock and patched, same deck."""
    pytest.importorskip("pptx")
    etree = pytest.importorskip("lxml.etree")
    source = _library()
    decks = {}
    for name in ("stock", "patched"):
        work = tmp_path / name
        shutil.copytree(source, work / "node_modules" / "pptxgenjs", symlinks=True)
        if name == "patched":
            patch.main(["patch", str(work / "node_modules" / "pptxgenjs")])
        env = {**os.environ, "NODE_PATH": os.pathsep.join([str(work / "node_modules"), str(source.parent)])}
        (work / "deck.js").write_text(DECK, encoding="utf-8")
        (work / "read.py").write_text(READ, encoding="utf-8")
        subprocess.run(["node", "deck.js", "deck.pptx"], cwd=work, check=True, env=env)
        decks[name] = json.loads(subprocess.run(
            [sys.executable, "read.py", "deck.pptx"], cwd=work, check=True, capture_output=True, text=True,
        ).stdout)

    for before, after in zip(decks["stock"], decks["patched"]):
        assert {k: before[k] for k in ("type", "cats", "depth", "series")} == {
            k: after[k] for k in ("type", "cats", "depth", "series")
        }
        assert sorted(re.findall(r"<c:symbol val=\"(\w+)\"/>", before["xml"])) == sorted(
            re.findall(r"<c:symbol val=\"(\w+)\"/>", after["xml"])
        )
        assert sorted(re.findall(r"<c:size val=\"(\d+)\"/>", before["xml"])) == sorted(
            re.findall(r"<c:size val=\"(\d+)\"/>", after["xml"])
        )

    line, bar, area, radar, multi, no_marker = [d["xml"] for d in decks["patched"]]
    for xml in (line, no_marker):
        for series in re.findall(r"<c:ser>.*?</c:ser>", xml, re.S):
            assert series.index("<c:marker>") < series.index("<c:dLbls>")
    for xml in (line, area, radar, no_marker):
        assert "invertIfNegative" not in xml
        assert "multiLvlStrRef" not in xml
        assert re.search(r"<c:cat>\s*<c:strRef>", xml)
    assert "invertIfNegative" in bar
    assert re.search(r"<c:cat>\s*<c:strRef>", bar)
    assert "multiLvlStrRef" in multi and multi.count("<c:lvl>") == 2
    assert decks["patched"][4]["depth"] == 2
    assert '<c:symbol val="none"/>' in no_marker

    # The two elements the schema refused in a series are no longer among its complaints.
    schema = etree.XMLSchema(etree.parse(str(SCHEMA)))
    for xml in (line, area, radar):
        schema.validate(etree.fromstring(xml.encode("utf-8")))
        assert not [e.message for e in schema.error_log if "invertIfNegative" in e.message or "'marker'" in e.message]
