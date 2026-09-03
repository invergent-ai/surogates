"""One task end-to-end through a fake harness client."""
from wsbench.client import Event
from wsbench.dataset import ManifestFile, Task
from wsbench.runner import (
    build_prompt,
    final_assistant_message,
    match_expected_outputs,
    run_task,
)


class FakeClient:
    """In-memory harness: uploads land in a dict, the agent 'writes' the
    files given at construction, the stream ends immediately."""

    def __init__(self, agent_writes: dict[str, bytes], status="completed"):
        self.workspace: dict[str, bytes] = {}
        self.agent_writes = agent_writes
        self.status = status
        self.prompts: list[str] = []

    async def create_session(self) -> str:
        return "sess-1"

    async def upload_file(self, session_id, local_path, filename, subdir=""):
        key = f"{subdir}/{filename}" if subdir else filename
        with open(local_path, "rb") as fh:
            self.workspace[key] = fh.read()
        return key

    async def send_message(self, session_id, content):
        self.prompts.append(content)
        self.workspace.update(self.agent_writes)
        return 1

    async def stream_events(self, session_id, after=0):
        for ev in [
            Event(1, "tool.call", {"name": "write_file", "arguments": {}}),
            Event(2, "llm.response", {"message": {"content": "wrote deps.md"}}),
        ]:
            if ev.id > after:
                yield ev

    async def get_session_status(self, session_id):
        return self.status

    async def get_workspace_tree(self, session_id):
        return [{"path": k, "size": len(v)} for k, v in self.workspace.items()]

    async def download_file(self, session_id, path):
        return self.workspace[path]


def _task(tmp_path):
    task_dir = tmp_path / "task_lite_clean_en" / "3"
    (task_dir / "data").mkdir(parents=True)
    (task_dir / "data" / "ab_pom.xml").write_text("<deps/>")
    return Task(
        task_id="3",
        persona="Backend Developer",
        instruction="Extract deps into deps.md.",
        difficulty="medium",
        output_files=("deps.md",),
        rubrics=("created?",),
        rubric_types=("Basic Evaluation",),
        tested_capabilities=(),
        manifest=(ManifestFile("pom.xml", "data/ab_pom.xml"),),
        local_dir=str(task_dir),
    )


def test_build_prompt_carries_persona_and_conventions(tmp_path):
    prompt = build_prompt(_task(tmp_path))
    assert "Backend Developer" in prompt
    assert "workdir/" in prompt
    assert "outputs/" in prompt
    assert "Extract deps into deps.md." in prompt


def test_match_expected_outputs_by_basename():
    assert match_expected_outputs(("a.md", "b.md"), ["outputs/a.md"]) == ["b.md"]
    assert match_expected_outputs(("a.md",), ["somewhere/deep/a.md"]) == []


async def test_run_task_uploads_prompts_and_collects(tmp_path):
    task = _task(tmp_path)
    client = FakeClient({"outputs/deps.md": b"# 43 deps"})
    task_dir = tmp_path / "run" / "tasks" / "3"
    task_dir.mkdir(parents=True)

    result = await run_task(client, task, str(task_dir))

    assert client.workspace["workdir/pom.xml"] == b"<deps/>"
    assert "workdir/" in client.prompts[0]
    assert result.terminal_status == "completed"
    assert result.error is None
    # Inputs are not collected; the agent's new file is.
    assert [c.workspace_path for c in result.collected] == ["outputs/deps.md"]
    assert result.missing_outputs == []
    collected = task_dir / "outputs" / "outputs" / "deps.md"
    assert collected.read_bytes() == b"# 43 deps"


async def test_run_task_reports_missing_outputs(tmp_path):
    task = _task(tmp_path)
    client = FakeClient({})  # agent writes nothing
    task_dir = tmp_path / "run" / "tasks" / "3"
    task_dir.mkdir(parents=True)

    result = await run_task(client, task, str(task_dir))
    assert result.missing_outputs == ["deps.md"]
    assert result.collected == []


async def test_run_task_records_failure_not_raises(tmp_path):
    class Exploding(FakeClient):
        async def create_session(self):
            raise RuntimeError("harness down")

    task = _task(tmp_path)
    task_dir = tmp_path / "run" / "tasks" / "3"
    task_dir.mkdir(parents=True)

    result = await run_task(Exploding({}), task, str(task_dir))
    assert result.terminal_status == "error"
    assert "harness down" in result.error
    assert result.missing_outputs == ["deps.md"]


def test_final_assistant_message_takes_last():
    events = [
        Event(1, "llm.response", {"message": {"content": "first"}}),
        Event(2, "llm.response", {"message": {"content": "last"}}),
    ]
    assert final_assistant_message(events) == "last"
