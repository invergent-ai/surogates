import pytest

from surogates.memory.manager import MemoryManager
from surogates.memory.store import MemoryStore
from surogates.memory.provider import MemoryProvider


class _Ext(MemoryProvider):
    def __init__(self, name): self._n = name
    @property
    def name(self): return self._n
    def is_available(self): return True
    def initialize(self, session_id="", **kw): ...
    def get_tool_schemas(self): return []


@pytest.fixture
def manager(tmp_path):
    return MemoryManager(MemoryStore(memory_dir=tmp_path / "mem"))


def test_internal_provider_does_not_consume_external_slot(manager):
    manager.add_provider(_Ext("channel"), internal=True)
    # An external provider must still be accepted afterwards.
    manager.add_provider(_Ext("honcho"))
    assert "channel" in manager.provider_names
    assert "honcho" in manager.provider_names


def test_second_external_still_rejected_with_internal_present(manager):
    manager.add_provider(_Ext("channel"), internal=True)
    manager.add_provider(_Ext("honcho"))
    manager.add_provider(_Ext("hindsight"))  # second external -> rejected
    assert "hindsight" not in manager.provider_names


def test_external_default_unchanged(manager):
    manager.add_provider(_Ext("honcho"))
    manager.add_provider(_Ext("hindsight"))
    assert "honcho" in manager.provider_names
    assert "hindsight" not in manager.provider_names
