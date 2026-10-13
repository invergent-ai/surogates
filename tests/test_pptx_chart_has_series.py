"""The office validator flags a chart part with no data series.

pptxgenjs writes one for an unknown chart type (``ChartType.col``) without an
error, and every viewer draws it as an empty frame.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/productivity/pptx/scripts/office"))
from helpers.pptx_chart import _check_chart_has_series  # noqa: E402

EMPTY = "<c:chartSpace><c:chart><c:title/><c:plotArea><c:layout/></c:plotArea></c:chart></c:chartSpace>"
BAR = "<c:chartSpace><c:chart><c:plotArea><c:barChart><c:ser><c:idx val='0'/></c:ser></c:barChart><c:valAx/></c:plotArea></c:chart></c:chartSpace>"


def test_chart_without_series_is_flagged():
    [problem] = _check_chart_has_series("ppt/charts/chart1.xml", EMPTY)
    assert "no ChartType.col" in problem


def test_chart_with_series_passes():
    assert _check_chart_has_series("ppt/charts/chart1.xml", BAR) == []
