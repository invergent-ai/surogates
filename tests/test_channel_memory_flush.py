import pytest

from surogates.memory.manager import MemoryManager
from surogates.memory.provider import MemoryProvider
from surogates.memory.store import MemoryStore


class _FlushProvider(MemoryProvider):
    def __init__(self):
        self.flushed = False

    @property
    def name(self): return "flushy"
    def is_available(self): return True
    def initialize(self, session_id="", **kw): return None
    def get_tool_schemas(self): return []

    async def flush(self):
        self.flushed = True


class _BoomProvider(MemoryProvider):
    @property
    def name(self): return "boom"
    def is_available(self): return True
    def initialize(self, session_id="", **kw): return None
    def get_tool_schemas(self): return []

    async def flush(self):
        raise RuntimeError("flush failed")


@pytest.mark.asyncio
async def test_flush_async_providers_awaits_provider_flush(tmp_path):
    manager = MemoryManager(MemoryStore(memory_dir=tmp_path / "mem"))
    provider = _FlushProvider()
    manager.add_provider(provider, internal=True)
    await manager.flush_async_providers()
    assert provider.flushed is True


@pytest.mark.asyncio
async def test_flush_isolates_provider_failure(tmp_path):
    manager = MemoryManager(MemoryStore(memory_dir=tmp_path / "mem"))
    good = _FlushProvider()
    manager.add_provider(_BoomProvider(), internal=True)
    manager.add_provider(good, internal=True)
    # A failing provider flush must not prevent others or raise.
    await manager.flush_async_providers()
    assert good.flushed is True
