"""A generated PDF survives inspection and Python's incidental cache files."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from surogates.harness.loop_artifact_completion import ArtifactCompletionMixin
from surogates.session.events import EventType


async def test_pdf_summary_keeps_inspected_pdf_and_excludes_generator_and_cache():
    session_id = uuid4()
    turn_id = str(uuid4())
    started = datetime(2026, 9, 10, 9, 43, 54, tzinfo=timezone.utc)
    modified = datetime(2026, 9, 10, 9, 44, 54, tzinfo=timezone.utc)
    pdf = "stirile-zilei-hotnews-2026-09-10.pdf"
    script = "make_stiri_pdf.py"
    events = [
        SimpleNamespace(type=EventType.LLM_REQUEST, data={"turn_id": turn_id}),
        SimpleNamespace(type=EventType.TOOL_CALL, data={
            "name": "write_file", "arguments": {"path": script},
        }),
        SimpleNamespace(type=EventType.TOOL_CALL, data={
            "name": "terminal", "arguments": {"command": (
                f"python3 {script} && pdfinfo {pdf} 2>/dev/null | head -12; "
                f'pdftotext -layout {pdf} - | grep -n "Diverse" -A3'
            )},
        }),
        SimpleNamespace(type=EventType.TOOL_CALL, data={
            "name": "terminal", "arguments": {"command": (
                f"pdfinfo {pdf} | grep -i pages; "
                f"pdftoppm -png -r 70 {pdf} preview && ls preview*"
            )},
        }),
    ]
    session = SimpleNamespace(
        id=session_id, channel="studio", config={"storage_bucket": "workspace"},
    )
    harness = ArtifactCompletionMixin()
    harness._turn_started_at = started
    harness._pending_iteration_summary_tasks = {}
    harness._turn_summarizer = None
    harness._store = AsyncMock()
    harness._store.get_session.return_value = session
    harness._store.get_events.side_effect = [[], events]
    harness._storage = AsyncMock()
    harness._storage.list_entries.return_value = [
        {"key": f"{session_id}/{path}", "size": size, "modified": modified}
        for path, size in [
            (script, 6785),
            (pdf, 68847),
            ("__pycache__/make_stiri_pdf.cpython-312.pyc", 7100),
        ]
    ]

    await harness._drain_and_emit_turn_summary(
        session_id=session_id, turn_id=turn_id,
        user_message="scrie-le intr-un pdf",
        final_message=f"Gata. Am făcut PDF-ul: {pdf}",
    )

    harness._store.emit_event.assert_awaited_once_with(
        session_id, EventType.TURN_SUMMARY, {
            "turn_id": turn_id,
            "recap": "",
            "rejected": [{"ref": script, "reason": "scaffolding"}],
            "artifacts": [{"kind": "file", "label": pdf, "ref": pdf}],
        },
    )
