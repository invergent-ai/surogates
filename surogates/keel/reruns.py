"""A rerun or a trial: a step Keel runs again, in a case that exists nowhere else.

A *rerun* runs an agent again on the files a past step read, as they were; a
*trial* runs a draft on inputs a builder typed.  Either is a step session of
a case whose id is the session's own, so the harness runs it as it runs any
step, and it reports to Keel's door under that id.

Its boundary folder (:func:`~surogates.keel.case_files.case_boundary_folder`)
holds the files it works on.  The folder's record, beside the workspace, says
whose it is and which kind: ``{"org_id", "rerun": <kind>}``.  Nothing that
names a case recorded without that mark is written to or removed, and a
rerun's files are copied from the *versions* of a real case
(:mod:`surogates.keel.versions`), which the real case keeps whatever its files
become.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from botocore.exceptions import ClientError
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from surogates.keel.case_files import CASE_ORG_OBJECT, case_boundary_folder, case_key, no_such_bucket
from surogates.keel.steps import StepContract
from surogates.keel.versions import SHA256_HEX, VERSIONS_FOLDER, case_versions
from surogates.memory.r2_store import MEMORY_TARGETS
from surogates.runtime.memory_protocol import memory_object_key

__all__ = [
    "MAX_RERUN_FILES",
    "CaseRecord",
    "NotDeleted",
    "RerunFile",
    "RerunRequest",
    "delete_boundary",
    "delete_checked",
    "mark_rerun",
    "may_hold_rerun",
    "missing_files",
    "read_case_record",
    "rerun_config",
    "rerun_memory_keys",
    "seed_files",
]

#: The most files one rerun is seeded with.
MAX_RERUN_FILES = 200


def _sha256_hex(value: str) -> str:
    if not SHA256_HEX.fullmatch(value):
        raise ValueError("not a SHA-256 digest")
    return value


class RerunFile(BaseModel):
    """One file a rerun starts with: the version of the original case with this hash, at this path."""

    model_config = ConfigDict(extra="ignore")

    path: str = Field(min_length=1, max_length=4096)
    sha256: Annotated[str, AfterValidator(_sha256_hex)]


class RerunRequest(BaseModel):
    """Keel's request to start a rerun or a trial.  Keys this runtime does not know are ignored."""

    model_config = ConfigDict(extra="ignore")

    org_id: UUID
    case_type: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    # The rerun's own id, which is also its case's: the runtime creates the session with exactly this one.
    session_id: UUID
    kind: Literal["rerun", "trial"]
    contract: StepContract
    files: list[RerunFile] = Field(default_factory=list, max_length=MAX_RERUN_FILES)
    # Whose versions seed *files*.
    of_case_id: UUID | None = None
    # Whether the agent's MCP servers, or those the contract names, are offered.
    mcp: bool = False
    # Whether the first message is written now; a trial may wait for its files.
    start: bool = True

    @model_validator(mode="after")
    def _consistent(self) -> RerunRequest:
        # The session's owning account is this case type's: it must be the contract's.
        if self.case_type != self.contract.case_type:
            raise ValueError("case_type differs from the contract's")
        if self.contract.case_id != self.session_id:
            raise ValueError("the contract is for another case")
        if self.files and self.of_case_id is None:
            raise ValueError("files need the case they come from")
        if self.of_case_id == self.session_id:
            raise ValueError("a rerun cannot be seeded from itself")
        paths = [file.path for file in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("two files at one path")
        return self


@dataclass(frozen=True, slots=True)
class CaseRecord:
    """What a case's boundary folder records of whose it is: the org, and the rerun kind when it is a rerun's."""

    org_id: UUID | None
    rerun: str | None


def _record_key(settings: Any, case_id: UUID) -> tuple[str, str]:
    bucket, folder = case_boundary_folder(settings, case_id)
    return bucket, folder + CASE_ORG_OBJECT


def _parsed(data: bytes) -> CaseRecord:
    """The record in *data*; ``CaseRecord(None, None)`` for one that is not ``{"org_id": <uuid>, ...}``."""
    try:
        record = json.loads(data)
        org_id = UUID(str(record["org_id"])) if isinstance(record, dict) else None
    except (ValueError, KeyError):
        return CaseRecord(None, None)
    if org_id is None:
        return CaseRecord(None, None)
    marked = record.get("rerun")
    return CaseRecord(org_id, None if marked is None else str(marked))


async def read_case_record(storage: Any, settings: Any, case_id: UUID) -> CaseRecord | None:
    """What *case_id*'s folder records, or ``None`` when it has no record.

    A record that cannot be read as an org's reads as ``CaseRecord(None, None)``,
    which no org or kind holds.  The store's own errors are raised as they are.
    """
    bucket, key = _record_key(settings, case_id)
    try:
        return _parsed(await storage.read(bucket, key))
    except KeyError:
        return None
    except ClientError as exc:
        if no_such_bucket(exc):
            return None
        raise


async def mark_rerun(storage: Any, settings: Any, case_id: UUID, org_id: UUID, kind: str) -> None:
    """Record *case_id*'s folder as *org_id*'s rerun of *kind*, making the bucket first."""
    bucket, key = _record_key(settings, case_id)
    await storage.create_bucket(bucket)
    await storage.write(bucket, key, json.dumps({"org_id": str(org_id), "rerun": kind}).encode())


def may_hold_rerun(record: CaseRecord | None, org_id: UUID, kind: str) -> bool:
    """Whether a folder with *record* may hold *org_id*'s rerun of *kind*: it has no record, or this one's."""
    return record is None or (record.org_id == org_id and record.rerun == kind)


async def missing_files(
    storage: Any, settings: Any, *, of_case_id: UUID, org_id: UUID, files: Sequence[RerunFile],
) -> list[int]:
    """The indexes of *files* whose version *of_case_id* does not hold.

    Every index when *of_case_id* has no record, is recorded under another
    org, or is a rerun's: a case that is not *org_id*'s keeps nothing for it.
    """
    if not files:
        return []
    record = await read_case_record(storage, settings, of_case_id)
    if record is None or record.org_id != org_id or record.rerun is not None:
        return list(range(len(files)))
    gone = set(await case_versions(settings, of_case_id).missing(storage, [file.sha256 for file in files]))
    return [index for index, file in enumerate(files) if file.sha256 in gone]


async def seed_files(
    storage: Any, settings: Any, *, of_case_id: UUID, case_id: UUID, files: Sequence[RerunFile],
) -> list[int]:
    """Copy each of *files* from *of_case_id*'s versions to its path in *case_id*'s workspace.

    Each is copied a piece at a time, through a scratch file that is removed
    whatever happens, and none is held whole.  The bucket is made first.
    Answers the indexes whose version is gone by the time it is read.
    """
    versions = case_versions(settings, of_case_id)
    bucket, _ = case_boundary_folder(settings, case_id)
    await storage.create_bucket(bucket)
    gone: list[int] = []
    for index, file in enumerate(files):
        handle, name = tempfile.mkstemp(prefix="keel-rerun-file-")
        os.close(handle)
        scratch = Path(name)
        try:
            try:
                await versions.download(storage, file.sha256, scratch, limit=None)
            except KeyError:
                gone.append(index)
                continue
            await storage.upload(bucket, case_key(settings, case_id, file.path), scratch)
        finally:
            scratch.unlink(missing_ok=True)
    return gone


def rerun_config(kind: str, agent: Mapping[str, Any]) -> dict[str, Any]:
    """The mark a rerun's session carries: its kind, and the agent's storage prefix, from which its memory is found."""
    return {"kind": kind, "memory_prefix": agent.get("storage_key_prefix")}


def rerun_memory_keys(memory_prefix: str, case_id: UUID) -> list[str]:
    """The object keys of a rerun's memory, as the worker keys them under the agent's storage prefix.

    An empty prefix is a prefix: the keys then start with ``/boundaries/``.
    """
    return [
        memory_object_key(
            storage_key_prefix=memory_prefix, user_id=None, boundary=f"case:{case_id}", target=target,
        )
        for target in MEMORY_TARGETS
    ]


class NotDeleted(Exception):
    """The store answered a delete without doing it: the objects are still there, and a retry must run."""


async def delete_checked(storage: Any, bucket: str, key: str) -> bool:
    """Delete the object at *key*, and look: whether it was there.

    An S3 backend answers a refused delete with nothing, so a purge that took
    that for done would answer 200 and leave what it promised to remove.
    Raises :class:`NotDeleted` when the object is still there.
    """
    if not await storage.exists(bucket, key):
        return False
    await storage.delete(bucket, key)
    if await storage.exists(bucket, key):
        raise NotDeleted
    return True


async def _prefix_deleted(storage: Any, bucket: str, prefix: str) -> int:
    """Delete every object under *prefix*, and look: how many.  :class:`NotDeleted` when any is left."""
    deleted = await storage.delete_prefix(bucket, prefix)
    if await storage.list_keys(bucket, prefix):
        raise NotDeleted
    return deleted


async def delete_boundary(storage: Any, settings: Any, case_id: UUID) -> int:
    """Delete *case_id*'s boundary folder: its workspace, then its versions, then its record: how many objects.

    The record goes last, so a purge that failed part-way leaves the mark that
    tells a retry the folder is a rerun's.  A store deletes a prefix in
    listing order, a batch at a time, and the record sorts before the rest:
    one delete of the folder could take it first and leave files unmarked.
    Each delete is looked at after (:func:`delete_checked`): a store that
    refuses one is :class:`NotDeleted`, not a purge done.  A bucket not made
    yet holds nothing.
    """
    bucket, folder = case_boundary_folder(settings, case_id)
    deleted = 0
    try:
        deleted += await _prefix_deleted(storage, bucket, folder + "workspace/")
        deleted += await _prefix_deleted(storage, bucket, folder + VERSIONS_FOLDER)
        deleted += int(await delete_checked(storage, bucket, folder + CASE_ORG_OBJECT))
    except ClientError as exc:
        if not no_such_bucket(exc):
            raise
    return deleted
