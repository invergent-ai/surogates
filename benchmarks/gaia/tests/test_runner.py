import httpx
import pytest

from gaia_bench.client import Event
from gaia_bench.dataset import Task
from gaia_bench.runner import GAIA_FORMAT_BLOCK, build_prompt, run_task


def make_task(**kw) -> Task:
    base = dict(
        task_id="t1", question="What is 2+2?", level=1,
        final_answer="4", file_name="", file_path="",
    )
    base.update(kw)
    return Task(**base)


class FakeClient:
    """Scriptable stand-in for HarnessClient."""

    def __init__(self, batches, status="completed"):
        self._batches = list(batches)
        self._status = status
        self.created = 0
        self.uploads = []
        self.messages = []
        self.after_cursors = []

    async def create_session(self):
        self.created += 1
        return f"sess-{self.created}"

    async def upload_file(self, session_id, local_path, filename):
        self.uploads.append((session_id, filename))
        return filename

    async def send_message(self, session_id, content, attachments=None):
        self.messages.append((session_id, content, attachments))
        return 1

    async def get_session_status(self, session_id):
        return self._status

    async def stream_events(self, session_id, after=0):
        self.after_cursors.append(after)
        batch = self._batches.pop(0) if self._batches else []
        for ev in batch:
            yield ev


def resp(eid, text):
    return Event(id=eid, type="llm.response",
                 data={"message": {"content": text}})


def test_prompt_appends_format_block_without_altering_question():
    p = build_prompt(make_task())
    assert p.startswith("What is 2+2?")
    assert GAIA_FORMAT_BLOCK.strip() in p


def test_format_block_states_the_marker():
    assert "FINAL ANSWER" in GAIA_FORMAT_BLOCK


async def test_extracts_answer_from_last_llm_response():
    client = FakeClient([[resp(1, "thinking"), resp(2, "FINAL ANSWER: 4")]])
    result = await run_task(client, make_task())
    assert result.answer == "4"
    assert result.terminal_status == "completed"
    assert len(result.events) == 2


async def test_missing_marker_yields_none_answer():
    client = FakeClient([[resp(1, "It is four.")]])
    result = await run_task(client, make_task())
    assert result.answer is None


async def test_reconnects_after_stream_timeout_with_last_event_id():
    # First batch ends without session.done (simulating stream.timeout);
    # the runner must reconnect from the last seen id.
    client = FakeClient(
        [[resp(7, "still working")], [resp(8, "FINAL ANSWER: 4")]],
        status="active",
    )
    calls = {"n": 0}

    async def status(session_id):
        calls["n"] += 1
        return "active" if calls["n"] <= 1 else "completed"

    client.get_session_status = status
    result = await run_task(client, make_task())
    assert client.after_cursors == [0, 7]
    assert result.answer == "4"


async def test_reconnects_after_mid_stream_transport_error():
    # A slow model turn can go >30s without emitting an event, so the read
    # timeout fires MID-iteration while the session is still running
    # server-side. Events already received must be kept and the stream
    # re-opened from the cursor -- not recorded as a task error.
    client = FakeClient(
        [[resp(3, "working")], [resp(4, "FINAL ANSWER: 4")]],
        status="active",
    )
    orig_stream = client.stream_events
    raised = {"done": False}

    async def flaky(session_id, after=0):
        async for ev in orig_stream(session_id, after=after):
            yield ev
        if not raised["done"]:
            raised["done"] = True
            raise httpx.ReadTimeout("stream went quiet")

    client.stream_events = flaky
    calls = {"n": 0}

    async def status(session_id):
        calls["n"] += 1
        return "active" if calls["n"] <= 1 else "completed"

    client.get_session_status = status

    result = await run_task(client, make_task())
    assert result.error is None
    assert result.terminal_status == "completed"
    assert result.answer == "4"
    assert client.after_cursors == [0, 3]


async def test_failed_status_terminates_instead_of_hanging():
    client = FakeClient([[resp(1, "partial")]], status="failed")
    result = await run_task(client, make_task())
    assert result.terminal_status == "failed"


async def test_uploads_attachment_and_references_it(tmp_path):
    f = tmp_path / "sheet.xlsx"
    f.write_bytes(b"fake")
    client = FakeClient([[resp(1, "FINAL ANSWER: 4")]])
    task = make_task(file_name="sheet.xlsx", file_path=str(f))
    result = await run_task(client, task)
    assert client.uploads == [("sess-1", "sheet.xlsx")]
    _, _, attachments = client.messages[0]
    assert attachments[0]["filename"] == "sheet.xlsx"
    assert result.answer == "4"


async def test_no_upload_when_task_has_no_file(tmp_path):
    client = FakeClient([[resp(1, "FINAL ANSWER: 4")]])
    await run_task(client, make_task())
    assert client.uploads == []
    _, _, attachments = client.messages[0]
    assert attachments is None


async def test_each_task_gets_a_fresh_session():
    client = FakeClient([[resp(1, "FINAL ANSWER: 4")],
                         [resp(1, "FINAL ANSWER: 5")]])
    r1 = await run_task(client, make_task(task_id="a"))
    r2 = await run_task(client, make_task(task_id="b"))
    assert r1.session_id != r2.session_id
    assert client.created == 2
