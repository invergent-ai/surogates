from surogates.harness.context_files import load_soul_md, truncate_context


class _Bundle:
    def __init__(self, files: dict[str, str]):
        self._files = files

    async def read_text(self, name: str) -> str:
        if name not in self._files:
            raise LookupError(name)
        return self._files[name]


async def test_an_identity_over_the_limit_is_cut_without_sending_the_model_to_file_tools():
    soul = "a" * 15_000 + "MIDDLE" + "z" * 6_000

    out = await load_soul_md(_Bundle({"SOUL.md": soul}))

    assert "truncated SOUL.md" in out
    assert "MIDDLE" not in out
    assert "file tools" not in out


async def test_an_identity_under_the_limit_is_kept_whole():
    soul = "a" * 19_000

    assert await load_soul_md(_Bundle({"SOUL.md": soul})) == soul


def test_a_workspace_file_still_points_at_file_tools():
    assert "Use file tools" in truncate_context("x" * 30_000, filename="notes.md")
