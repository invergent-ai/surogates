"""One task end-to-end through a fake harness client."""
from dabbench import runner
from dabbench.client import Event
from dabbench.dataset import Task
from dabbench.runner import build_prompt, extract_final_answer, run_task


class FakeClient:
    def __init__(self, final_message, status="completed"):
        self.workspace: dict[str, bytes] = {}
        self.final_message = final_message
        self.status = status
        self.prompts: list[str] = []

    async def create_session(self):
        return "sess-1"

    async def upload_file(self, session_id, local_path, filename, subdir=""):
        key = f"{subdir}/{filename}" if subdir else filename
        with open(local_path, "rb") as fh:
            self.workspace[key] = fh.read()
        return key

    async def send_message(self, session_id, content):
        self.prompts.append(content)
        return 1

    async def stream_events(self, session_id, after=0):
        for ev in [
            Event(1, "tool.call", {"name": "terminal", "arguments": {}}),
            Event(2, "llm.response", {"message": {"content": self.final_message}}),
        ]:
            if ev.id > after:
                yield ev

    async def get_session_status(self, session_id):
        return self.status


def _task():
    return Task(
        task_id="5",
        question="Which issuing country has the highest number of transactions?",
        guidelines="Answer must be just the country code",
        level="easy",
        answer="",
    )


def _fake_context(tmp_path, monkeypatch):
    files = []
    for name in ("manual.md", "payments.csv"):
        p = tmp_path / name
        p.write_text(name)
        files.append(str(p))
    monkeypatch.setattr(runner, "context_paths", lambda: files)


def test_build_prompt_carries_question_guidelines_template():
    prompt = build_prompt(_task())
    assert "Which issuing country" in prompt
    assert "just the country code" in prompt
    assert "FINAL ANSWER:" in prompt
    assert "manual.md" in prompt


def test_extract_final_answer_variants():
    assert extract_final_answer("blah\nFINAL ANSWER: NL") == "NL"
    assert extract_final_answer("FINAL ANSWER: [42.50]") == "42.50"
    assert extract_final_answer("final answer: a, b, c") == "a, b, c"
    # The last occurrence wins; absence is None.
    assert extract_final_answer("FINAL ANSWER: X\nFINAL ANSWER: Y") == "Y"
    assert extract_final_answer("no answer here") is None


async def test_run_task_uploads_context_and_extracts(tmp_path, monkeypatch):
    _fake_context(tmp_path, monkeypatch)
    client = FakeClient("Done.\nFINAL ANSWER: NL")

    result = await run_task(client, _task())

    assert client.workspace["data/manual.md"] == b"manual.md"
    assert client.workspace["data/payments.csv"] == b"payments.csv"
    assert "FINAL ANSWER" in client.prompts[0]
    assert result.answer == "NL"
    assert result.terminal_status == "completed"
    assert result.error is None


async def test_run_task_records_failure_not_raises(tmp_path, monkeypatch):
    _fake_context(tmp_path, monkeypatch)

    class Exploding(FakeClient):
        async def create_session(self):
            raise RuntimeError("harness down")

    result = await run_task(Exploding(""), _task())
    assert result.terminal_status == "error"
    assert "harness down" in result.error
    assert result.answer is None
