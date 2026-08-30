import pytest

from gaia_bench.dataset import Task, make_split, resolve_attachment


def test_split_is_deterministic():
    ids = [f"task-{i}" for i in range(165)]
    dev_a, hold_a = make_split(ids)
    dev_b, hold_b = make_split(ids)
    assert dev_a == dev_b
    assert hold_a == hold_b


def test_split_sizes_and_disjointness():
    ids = [f"task-{i}" for i in range(165)]
    dev, hold = make_split(ids, dev_size=110)
    assert len(dev) == 110
    assert len(hold) == 55
    assert set(dev).isdisjoint(set(hold))
    assert set(dev) | set(hold) == set(ids)


def test_split_ignores_input_order():
    ids = [f"task-{i}" for i in range(165)]
    dev_a, _ = make_split(ids)
    dev_b, _ = make_split(list(reversed(ids)))
    assert sorted(dev_a) == sorted(dev_b)


class TestResolveAttachment:
    """GAIA's file_path is a path INSIDE the HF repo, not a local file.

    Passing it straight to open() raised FileNotFoundError for every
    attachment task, which the runner recorded as infra_error -- 27 of
    110 dev tasks written off as infrastructure noise rather than
    measured.
    """

    def test_returns_none_for_task_without_file(self):
        t = Task(task_id="a", question="q", level=1, final_answer="4",
                 file_name="", file_path="")
        assert resolve_attachment(t) is None

    def test_downloads_repo_relative_path(self, monkeypatch):
        seen = {}

        def fake_download(**kw):
            seen.update(kw)
            return "/cache/x.xlsx"

        monkeypatch.setattr(
            "huggingface_hub.hf_hub_download", fake_download
        )
        t = Task(task_id="a", question="q", level=1, final_answer="4",
                 file_name="x.xlsx", file_path="2023/validation/x.xlsx")
        assert resolve_attachment(t, hf_token="tok") == "/cache/x.xlsx"
        assert seen["repo_id"] == "gaia-benchmark/GAIA"
        assert seen["repo_type"] == "dataset"
        assert seen["filename"] == "2023/validation/x.xlsx"

    def test_prefers_an_existing_local_path(self, tmp_path, monkeypatch):
        real = tmp_path / "x.xlsx"
        real.write_bytes(b"data")

        def boom(**kw):
            raise AssertionError("must not download when file is local")

        monkeypatch.setattr("huggingface_hub.hf_hub_download", boom)
        t = Task(task_id="a", question="q", level=1, final_answer="4",
                 file_name="x.xlsx", file_path=str(real))
        assert resolve_attachment(t) == str(real)

    def test_falls_back_to_file_name_when_path_empty(self, monkeypatch):
        seen = {}

        def fake_download(**kw):
            seen.update(kw)
            return "/cache/y.pdb"

        monkeypatch.setattr(
            "huggingface_hub.hf_hub_download", fake_download
        )
        t = Task(task_id="a", question="q", level=1, final_answer="4",
                 file_name="y.pdb", file_path="")
        assert resolve_attachment(t) == "/cache/y.pdb"
        assert seen["filename"] == "2023/validation/y.pdb"


def test_task_is_frozen():
    t = Task(
        task_id="a", question="q", level=1,
        final_answer="42", file_name="", file_path="",
    )
    assert t.level == 1
    assert not t.file_name


class TestUnsupportedCapability:
    """Audio tasks are skipped from execution but not from the denominator."""

    def _task(self, file_name: str):
        from gaia_bench.dataset import Task
        return Task(task_id="t", question="q", level=1, final_answer="a",
                    file_name=file_name, file_path="")

    def test_audio_attachments_are_unsupported(self) -> None:
        from gaia_bench.dataset import needs_unsupported_capability
        for name in ("a.mp3", "b.WAV", "c.m4a", "d.flac"):
            assert needs_unsupported_capability(self._task(name)) is True, name

    def test_everything_else_is_supported(self) -> None:
        """This is a capability boundary, not a difficulty filter -- widening
        it to anything the agent finds hard would make the score meaningless."""
        from gaia_bench.dataset import needs_unsupported_capability
        for name in ("a.pdf", "b.xlsx", "c.png", "d.zip", "e.pdb", "", "f.jsonld"):
            assert needs_unsupported_capability(self._task(name)) is False, name
