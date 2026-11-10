import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.devices import operations as operations_module
from surogates.devices.operations import DeviceOperations, FolderPatience


def test_patience_without_notice_is_refused():
    with pytest.raises(ValueError, match="patience needs a notice"):
        DeviceOperations(object(), object(), patience=object())


async def test_a_folder_freed_near_the_limit_still_leaves_the_rest_of_the_request_its_whole_within(monkeypatch):
    monkeypatch.setattr(operations_module, "FOLDER_WAIT_S", 0.2)
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(1.0) as deadline:
        patience = FolderPatience(deadline, 1.0)
        await asyncio.sleep(0.5)  # past the limit a folder wait could hold it to
        freed = loop.time()
        patience.freed()
        assert deadline.when() >= freed + 1.0


class _Notice:
    """A notice whose first word of a folder wait takes *slow_s*, as a busy database answers."""

    def __init__(self, slow_s: float = 0.0) -> None:
        self.slow_s = slow_s

    async def folder_held(self, _request):
        await asyncio.sleep(self.slow_s)

    async def folder_free(self, _request, **_kwargs):
        pass

    async def away(self, _request):
        pass

    async def back(self, _request, **_kwargs):
        pass


RECHECK = 0.1


async def _watched(notice: _Notice, polls: list, answered_after_s: float) -> dict:
    """Wait an operation out under a deadline of a twentieth of a recheck, the app saying it waits for its folder.

    Each poll of the folder wait takes the next of *polls*: (how long it takes, what it says).
    """
    loop = asyncio.get_running_loop()
    waiter = loop.create_future()
    loop.call_later(answered_after_s, waiter.set_result, {"ok": True})
    async with asyncio.timeout(RECHECK / 20) as deadline:
        ops = DeviceOperations(
            object(), object(), recheck_interval_s=RECHECK, notice=notice, patience=FolderPatience(deadline, RECHECK / 20),
        )

        async def folder_held(_operation_id):
            took, said = polls.pop(0) if polls else (0.0, True)
            await asyncio.sleep(took)
            return said

        async def online(_presence, _device_id):
            return True

        ops._folder_held, ops._online = folder_held, online
        return await ops._wait_watching(waiter, SimpleNamespace(device_id=uuid4()), uuid4())


async def test_a_slow_first_word_of_a_folder_wait_does_not_run_its_deadline_out():
    assert await _watched(_Notice(slow_s=1.5 * RECHECK), [], answered_after_s=4 * RECHECK) == {"ok": True}


async def test_a_poll_that_times_out_during_a_folder_wait_does_not_run_its_deadline_out():
    # The second poll waits out its whole Redis patience and says nothing; the third says the wait goes on.
    polls = [(0.0, True), (RECHECK, None), (0.0, True)]
    assert await _watched(_Notice(), polls, answered_after_s=5 * RECHECK) == {"ok": True}
