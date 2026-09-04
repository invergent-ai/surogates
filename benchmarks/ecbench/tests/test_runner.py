"""One episode end-to-end through a fake harness client."""
import json

from ecbench import runner
from ecbench.client import Event
from ecbench.runner import build_prompt, run_episode
from ecbench.staging import StagedFile


class FakeClient:
    """In-memory harness: uploads land in a dict; the 'agent' leaves the
    given artifacts in the workspace; the stream ends immediately."""

    def __init__(self, agent_writes: dict[str, bytes], status="completed"):
        self.workspace: dict[str, bytes] = {}
        self.agent_writes = agent_writes
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
        self.workspace.update(self.agent_writes)
        return 1

    async def stream_events(self, session_id, after=0):
        for ev in [
            Event(1, "tool.call", {"name": "terminal", "arguments": {}}),
            Event(2, "llm.response", {"message": {"content": "finalized"}}),
        ]:
            if ev.id > after:
                yield ev

    async def get_session_status(self, session_id):
        return self.status

    async def get_workspace_tree(self, session_id):
        return [{"path": k, "size": len(v)} for k, v in self.workspace.items()]

    async def download_file(self, session_id, path):
        return self.workspace[path]


def _fake_plan(tmp_path):
    src = tmp_path / "env.py"
    src.write_text("ENV")
    ecsim = tmp_path / "ecsim.py"
    ecsim.write_text("CLI")
    return [
        StagedFile(str(src), "sim/tools", "env.py", "sim/tools/env.py", 3),
        StagedFile(str(ecsim), "", "ecsim.py", "ecsim.py", 3),
    ]


def test_build_prompt_carries_protocol():
    prompt = build_prompt(max_days=30, balance=100000.0)
    assert "ecsim.py init --max-days 30" in prompt
    assert "finalize" in prompt
    assert "wait_for_next_day" in prompt
    assert '{"tool_name"' in prompt


async def test_run_episode_uploads_and_collects(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "stage_plan", lambda: _fake_plan(tmp_path))
    final = json.dumps({"final_assets": 1}).encode()
    client = FakeClient({"final_state.json": final, "calls.jsonl": b"{}\n"})
    episode_dir = tmp_path / "run" / "episodes" / "01"

    result = await run_episode(client, 1, str(episode_dir), max_days=30)

    assert client.workspace["sim/tools/env.py"] == b"ENV"
    assert client.workspace["ecsim.py"] == b"CLI"
    assert "--max-days 30" in client.prompts[0]
    assert result.terminal_status == "completed"
    assert sorted(result.artifacts) == ["calls.jsonl", "final_state.json"]
    assert (episode_dir / "final_state.json").read_bytes() == final
    assert any("sim_state.pkl not present" in n for n in result.collect_notes)


async def test_run_episode_records_failure_not_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "stage_plan", lambda: _fake_plan(tmp_path))

    class Exploding(FakeClient):
        async def create_session(self):
            raise RuntimeError("harness down")

    result = await run_episode(
        Exploding({}), 1, str(tmp_path / "e"), max_days=30
    )
    assert result.terminal_status == "error"
    assert "harness down" in result.error
    assert result.artifacts == []
