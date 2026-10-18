"""A virtual session: an agent editing one document turn by turn, each turn's save checked for the turn's last edit.

A turn makes its edits, then saves as the harness's turn end does: flush the agent's page, force a save, wait for the
save's callback. The saved file must hold the turn's last marker; any other save is a lost one.
"""

from __future__ import annotations

import asyncio
import random
import secrets
import time
from dataclasses import dataclass
from typing import Any

from oload.clients import Fleet, FleetFull, PageGone, RequestRefused, RuntimeOffice, RuntimeOfficeError
from oload.docserver import CallbackState, callback_url, file_url, forcesave
from oload.editor import editor_config
from oload.events import Cause, Event, EventLog, What
from oload.markers import has_marker
from oload.pace import Pace, Workload
from oload.settings import Settings
from oload.workloads import OUTLINE_READ, DeckChanged, edit_ops, fixture, pick_kind, slide_read

CALLBACK_WAIT_S = 30.0  # as long as the harness's turn end waits for a document's save


class _Ended(Exception):
    """The session cannot go on: its page could not be opened, or was lost twice."""


@dataclass(slots=True)
class _Page:
    session_id: str
    user_id: str
    user_name: str
    plugins: bool
    client: RuntimeOffice | None = None  # once an open was tried, until the page is given up
    id: str | None = None  # while open


class VirtualSession:
    """Session *index* of a step: its document is the mix's *index*-th kind, its key ``load-<run>-<index>-<nonce>``.

    A chat session also opens a watching page on the document, as a person's editor: it only stays open.
    """

    def __init__(self, index: int, workload: Workload, fleet: Fleet, settings: Settings, state: CallbackState,
                 log: EventLog, rng: random.Random, *, run_id: str, pace: Pace | None = None) -> None:
        self.index = index
        self.workload = workload
        self.fleet = fleet
        self.settings = settings
        self.state = state
        self.log = log
        self.pace = pace if pace is not None else Pace(workload, rng)
        self.kind = pick_kind(index)
        self._nonce = secrets.token_hex(4)  # a key the document server has never seen, whatever was run before
        self.doc_key = f"load-{run_id}-{index}-{self._nonce}"
        self.agent = _Page(f"agent-{index}", f"agent-{index}", f"Load agent {index}", plugins=False)
        self.watcher = (_Page(f"person-{index}", f"person-{index}", f"Load person {index}", plugins=True)
                        if workload == "chat" else None)
        self._held: list[RuntimeOffice] = []  # one per page slot placed on the fleet
        self._edits = 0
        self._losses = 0
        self._slide: dict[str, Any] | None = None  # a deck's slide read, from its outline
        # This turn's markers, any of which in the save shows it holds the turn's last edit: the last one applied,
        # then those whose edit may have applied (it failed past sending).
        self._expect: list[str] = []
        self._edited = False  # an edit applied this turn

    async def run(self, until: float, *, turns: int | None = None) -> None:
        """Turn after turn until *until*, a time.monotonic() deadline, or *turns* turns; then close the pages.

        Raises FleetFull when the fleet has no room for the session's pages.
        """
        client = self.fleet.place(1 if self.watcher is None else 2)
        self._held = [client] * (1 if self.watcher is None else 2)
        self.state.add_document(self.doc_key, self.kind, fixture(self.kind))
        try:
            if not await self._open(self.agent, client):
                return
            if self.watcher is not None:
                await self._open(self.watcher, client)
            turn = 0
            while time.monotonic() < until:
                await self._turn(turn, until)
                turn += 1
                if turns is not None and turn >= turns:
                    break
                await asyncio.sleep(max(0.0, min(self.pace.idle_s(), until - time.monotonic())))
        except _Ended:
            pass
        finally:
            await self._close()

    async def _turn(self, turn: int, until: float) -> None:
        self._expect, self._edited = [], False
        for i in range(self.pace.edits_in_turn()):
            if i:
                gap = self.pace.gap_s()
                if time.monotonic() + gap >= until:
                    break  # the turn ends here: what it edited is saved
                await asyncio.sleep(gap)
            await self._edit(f"load-{self.index}-{self._nonce}-t{turn}-e{i}")
        if await self._flush():
            await self._save(turn)

    async def _edit(self, marker: str) -> None:
        n, self._edits = self._edits, self._edits + 1
        sent = False
        try:
            page = await self._page()
            assert page.client is not None and page.id is not None
            read = None
            if self.kind == "pptx":  # read before each edit, as an agent does: the slide's hash guards the change
                if self._slide is None:
                    self._slide = slide_read((await page.client.read(page.id, OUTLINE_READ)).value)
                read = (await page.client.read(page.id, self._slide)).value
            ops = edit_ops(self.kind, marker, n, read)
            sent = True
            answered = await page.client.edit(page.id, ops)
        except (RequestRefused, DeckChanged) as exc:
            self._error(exc)
            return
        except RuntimeOfficeError as exc:
            self._failed("edit", exc)
            if isinstance(exc, PageGone):
                self._lost()
            elif sent:
                self._expect.append(marker)  # it may have applied
            return
        applied = answered.value.get("applied") is True
        self._event("edit", applied, answered.seconds, None if applied else "server",
                    detail=None if applied else str(answered.value)[:300])
        if applied:
            self._expect, self._edited = [marker], True

    async def _flush(self) -> bool:
        """Whether the turn's forcesave goes: as the harness, only once the page sent everything, or if it is gone."""
        try:
            page = await self._page()
            assert page.client is not None and page.id is not None
            flushed = await page.client.flush(page.id)
        except RequestRefused as exc:
            self._error(exc)
            return False
        except PageGone as exc:
            self._failed("flush", exc)
            self._lost()
            return True  # what it sent is on the document server, and the forcesave takes it
        except RuntimeOfficeError as exc:
            self._failed("flush", exc)
            return False
        self._event("flush", flushed.value, flushed.seconds, None if flushed.value else "server")
        return flushed.value

    async def _save(self, turn: int) -> None:
        userdata = f"turn-{turn}"
        sent = time.monotonic()
        answer = await forcesave(self.settings, self.doc_key, userdata)
        if answer.outcome != "answered":
            self._event("forcesave", False, answer.seconds, "timeout" if answer.outcome == "timeout" else "server",
                        detail=f"{answer.outcome} (HTTP {answer.http_status})")
            return
        if answer.error == 4:  # nothing unsaved: the turn's edits never reached the document server
            self._event("forcesave", not self._edited, answer.seconds, "lost" if self._edited else None,
                        detail="nothing to save" if self._edited else None)
            return
        if answer.error != 0:
            self._event("forcesave", False, answer.seconds, "server", detail=f"error {answer.error}")
            return
        self._event("forcesave", True, answer.seconds)
        callback = await self.state.wait_for(self.doc_key, userdata, CALLBACK_WAIT_S)
        if callback is None:
            self._event("callback", False, time.monotonic() - sent, "timeout", began=sent)
            return
        settled = callback.settled_mono if callback.settled_mono is not None else time.monotonic()
        if callback.error is not None or callback.data is None:  # its file not fetched, or a failed save (status 7)
            self._event("callback", False, settled - sent, "server", began=sent,
                        detail=callback.error or f"status {callback.status}")
            return
        self._event("callback", True, settled - sent, began=sent)
        if self._expect:
            started = time.monotonic()
            # Off the event loop: reading a saved document must not delay the other sessions' timings.
            found = await asyncio.to_thread(_holds_any, self.kind, callback.data, tuple(self._expect))
            self._event("marker", found, time.monotonic() - started, None if found else "lost",
                        detail=None if found else f"turn {turn}'s save lacks {self._expect[0]}")
        callback.release()

    async def _open(self, page: _Page, client: RuntimeOffice, *, again: bool = False) -> bool:
        page.client = client
        config = editor_config(
            self.settings, doc_key=self.doc_key, kind=self.kind, user_id=page.user_id, user_name=page.user_name,
            file_url=file_url(self.settings, self.doc_key), callback_url=callback_url(self.settings, self.doc_key),
            plugins=page.plugins,
        )
        try:
            opened = await client.open(self.doc_key, page.session_id, config, self.settings.ds_url)
        except RequestRefused as exc:
            self._error(exc)
            return False
        except RuntimeOfficeError as exc:
            # Its slot stays counted until the session ends: a page that timed out or dropped may still be opening.
            self._failed("open", exc)
            return False
        page.id = opened.value
        self._event("open", True, opened.seconds, detail="again" if again else None)
        return True

    async def _page(self) -> _Page:
        """The agent's page; lost, it opens again, on a client with room, and the session starts over on it."""
        page = self.agent
        if page.id is not None:
            return page
        try:
            client = self.fleet.place(1)
        except FleetFull as exc:
            self._event("open", False, 0.0, "client", detail=str(exc))
            raise _Ended from None
        self._held.append(client)
        if not await self._open(page, client, again=True):
            raise _Ended
        return page

    def _lost(self) -> None:
        """The agent's page is gone, and its slot with it: opened again at its next call, once.

        What it edited this turn is no longer checked: with no editor left, the document server saves it on its own,
        and a page opened again may load the document from its input.
        """
        client = self.agent.client
        assert client is not None
        self.agent.client, self.agent.id = None, None
        self._slide, self._expect, self._edited = None, [], False
        self._held.remove(client)
        self.fleet.release(client)
        self._losses += 1
        if self._losses > 1:
            raise _Ended

    async def _close(self) -> None:
        try:
            for page in (self.agent, self.watcher):
                if page is None or page.client is None:
                    continue
                try:
                    await page.client.close(page.id or f"{self.doc_key}.{page.session_id}")  # runtime-office's id
                except (RuntimeOfficeError, RequestRefused):
                    pass
        finally:
            for client in self._held:
                self.fleet.release(client)
            self._held = []
            # Saves that came too late to be checked: nothing reads them any more.
            for callback in self.state.callbacks:
                if callback.doc_key == self.doc_key:
                    callback.release()

    def _failed(self, what: What, exc: RuntimeOfficeError) -> None:
        self._event(what, False, exc.seconds, exc.cause, detail=str(exc)[:300])

    def _error(self, exc: Exception) -> None:
        self._event("error", False, 0.0, "tool", detail=str(exc)[:300])

    def _event(self, what: What, ok: bool, seconds: float, cause: Cause | None = None, *,
               began: float | None = None, detail: str | None = None) -> None:
        """Log *what*; *began* is its time.monotonic() start, by default *seconds* before now."""
        now = time.monotonic()
        began = now - seconds if began is None else began
        self.log.add(Event(self.index, self.kind, what, ok, seconds, cause, time.time() - (now - began), detail))


def _holds_any(kind: str, data: bytes, markers: tuple[str, ...]) -> bool:
    return any(has_marker(kind, data, marker) for marker in markers)
