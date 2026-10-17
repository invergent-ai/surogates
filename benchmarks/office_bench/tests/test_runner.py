"""One task end to end through a fake harness, and the runner's path handling."""
import json

import pytest

from obench import runner
from obench.client import Event, HarnessError
from obench.runner import FatalRunError, doc_path, edited_paths, run_task, write_rollout
from obench.tasks import by_id


class FakeHarness:
    """In-memory harness. The agent's turn 'edits' the target; the save lands on the second download after it."""

    def __init__(self, edits: dict[str, bytes], status: str = "completed", events: list[Event] | None = None):
        self.workspace: dict[str, bytes] = {}
        self.edits = edits
        self.status = status
        self.prompts: list[str] = []
        self.created: list[tuple[str, dict]] = []
        self.downloads = 0
        self.events = events

    async def create_session(self, channel, config):
        self.created.append((channel, config))
        return "sess-1"

    async def upload_file(self, session_id, local_path, filename, subdir=""):
        with open(local_path, "rb") as fh:
            self.workspace[filename] = fh.read()
        return filename

    async def send_message(self, session_id, content):
        self.prompts.append(content)
        return 1

    async def stream_events(self, session_id, after=0):
        events = self.events if self.events is not None else [
            Event(1, "user.message", {"content": "x"}),
            Event(2, "tool.call", {"name": "office_edit", "arguments": {"path": "/workspace/" + next(iter(self.edits)), "ops": []}}),
            Event(3, "llm.response", {"model": "m", "message": {"content": "done"}}),
        ]
        for ev in events:
            if ev.id > after:
                yield ev

    async def get_session_status(self, session_id):
        return self.status

    async def delete_session(self, session_id):
        self.deleted = session_id

    async def get_session(self, session_id):
        return {"status": self.status, "channel": "studio", "agent_id": "a"}

    async def get_workspace_tree(self, session_id):
        return [{"path": k, "size": 0} for k in self.workspace]

    async def download_file(self, session_id, path):
        self.downloads += 1
        if self.downloads >= 2:
            self.workspace.update(self.edits)  # the save lands after the turn
        if path not in self.workspace:
            raise HarnessError("404", status=404)
        return self.workspace[path]


@pytest.fixture(autouse=True)
def fast_saves(monkeypatch):
    monkeypatch.setattr(runner, "SAVE_SETTLE_S", 0.0)
    monkeypatch.setattr(runner, "SAVE_POLL_S", 0.0)


def test_doc_path_normalizes_what_office_edit_was_given():
    assert doc_path("/workspace/a/b.docx") == "a/b.docx"
    assert doc_path("./b.docx") == "b.docx"
    assert doc_path(" b.docx ") == "b.docx"


def test_edited_paths_reads_string_and_dict_arguments():
    events = [
        Event(1, "tool.call", {"name": "office_edit", "arguments": {"path": "a.docx"}}),
        Event(2, "tool.call", {"name": "office_edit", "arguments": json.dumps({"path": "./a.docx"})}),
        Event(3, "tool.call", {"name": "office_read", "arguments": {"path": "b.docx"}}),
        Event(4, "tool.call", {"name": "office_edit", "arguments": {"path": "c.xlsx"}}),
    ]
    assert edited_paths(events) == ["a.docx", "c.xlsx"]


async def test_edit_task_uploads_waits_for_the_save_and_collects(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICEBENCH_OPS_USER_ID", "u1")
    monkeypatch.setenv("OFFICEBENCH_OPS_USERNAME", "surogate")
    task = by_id()["docx-reword"]
    fake = FakeHarness({"quarterly-report.docx": b"PK-edited"})
    r = await run_task(fake, task, str(tmp_path))
    assert fake.created == [("studio", {"ops": {"user_id": "u1", "username": "surogate"}})]
    assert fake.prompts == [task.prompt]
    assert r.terminal_status == "completed" and r.error is None
    assert r.save["landed"] == {"quarterly-report.docx": True}
    [doc] = r.collected
    assert doc["input"] and doc["changed"]
    assert (tmp_path / "documents" / "quarterly-report.docx").read_bytes() == b"PK-edited"
    assert r.released == "yes" and fake.deleted == "sess-1"  # only after the documents are downloaded
    write_rollout(str(tmp_path), task, r)
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["session"]["channel"] == "studio"
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 3


async def test_keep_sessions_keeps_it(tmp_path):
    fake = FakeHarness({"quarterly-report.docx": b"PK-edited"})
    r = await run_task(fake, by_id()["docx-reword"], str(tmp_path), release=False)
    assert r.released.startswith("kept") and not hasattr(fake, "deleted")


async def test_no_edit_means_no_save_wait(tmp_path):
    task = by_id()["docx-reword"]
    fake = FakeHarness({"other.docx": b"x"}, events=[Event(1, "llm.response", {"message": {"content": "no"}})])
    r = await run_task(fake, task, str(tmp_path))
    assert r.save == {"edited": [], "landed": {}, "waited_s": 0.0, "late_events": []}
    assert r.collected[0]["changed"] is False


async def test_a_refused_credential_stops_the_run(tmp_path):
    class Refusing(FakeHarness):
        async def create_session(self, channel, config):
            raise HarnessError("create_session failed (HTTP 403): reserved", status=403)

    with pytest.raises(FatalRunError):
        await run_task(Refusing({"a.docx": b""}), by_id()["docx-report"], str(tmp_path))


async def test_other_failures_are_recorded_not_raised(tmp_path):
    class Failing(FakeHarness):
        async def send_message(self, session_id, content):
            raise HarnessError("send_message failed (HTTP 422): bad", status=422)

    r = await run_task(Failing({"a.docx": b""}), by_id()["docx-report"], str(tmp_path))
    assert r.terminal_status == "error"
    assert "HTTP 422" in r.error


async def test_derived_input_is_staged_in_the_rollout(tmp_path):
    task = by_id()["xlsx-fix-formula"]
    fake = FakeHarness({"sales.xlsx": b"PK"})
    await run_task(fake, task, str(tmp_path))
    import openpyxl

    staged = openpyxl.load_workbook(tmp_path / "inputs" / "sales.xlsx")
    assert staged["Sales"]["D3"].value == "=B3+B2"
