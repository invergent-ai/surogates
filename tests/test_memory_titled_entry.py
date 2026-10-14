"""A titled memory entry is one record that adding again keeps current."""

import pytest

from surogates.memory.r2_store import R2MemoryStore
from surogates.memory.store import MemoryStore, titled_entry_index

RECORD = "FIȘA\nPacient: Vasile Ionescu\nRăspunde: pacientul"
UPDATED = "FIȘA\nPacient: Vasile Ionescu\nRăspunde: fiica"
# Holds RECORD as a substring, before it: a str.replace of RECORD edits this one.
QUOTING = "Note: copied from " + RECORD


def test_adding_a_record_with_the_same_title_finds_the_old_one():
    assert titled_entry_index(UPDATED, ["Prefers mornings", RECORD]) == 1


def test_a_different_title_is_a_new_entry():
    assert titled_entry_index("NOTE\nsomething", [RECORD]) is None


def test_a_long_first_line_is_prose_not_a_title():
    prose = "The user said they prefer to be contacted in the morning after breakfast, never late"
    assert titled_entry_index(prose + "\nmore", [prose + "\nolder"]) is None


def test_empty_content_has_no_title():
    assert titled_entry_index("   ", [RECORD]) is None


def test_disk_store_add_replaces_the_titled_entry_by_position(tmp_path):
    store = MemoryStore(tmp_path)
    store.load_from_disk()
    store.add("user", QUOTING)
    store.add("user", RECORD)

    result = store.add("user", UPDATED)

    assert result["success"]
    assert store.get_entries("user") == [QUOTING, UPDATED]


class _Backend:
    def __init__(self):
        self.objects = {}

    async def read(self, bucket, key):
        return self.objects[key]

    async def write(self, bucket, key, data):
        self.objects[key] = data


@pytest.mark.asyncio
async def test_r2_store_add_replaces_the_titled_entry_by_position():
    store = R2MemoryStore(
        backend=_Backend(), bucket="b", keys={"memory": "m", "user": "u"},
    )
    await store.load_from_r2()
    await store.add("user", QUOTING)
    await store.add("user", RECORD)

    await store.add("user", UPDATED)

    assert store.get_entries("user") == [QUOTING, UPDATED]
    await store.add("user", "NOTE\nnew")
    assert store.get_entries("user") == [QUOTING, UPDATED, "NOTE\nnew"]


def test_a_first_line_with_a_colon_is_a_note_not_a_title():
    from surogates.memory.store import merge_entry

    entries = ["Notes: likes mornings\nmore"]
    merged, replaced = merge_entry(entries, "Notes: likes mornings\nother")
    assert (merged, replaced) == ([*entries, "Notes: likes mornings\nother"], False)


def test_a_record_heading_still_replaces_its_record():
    from surogates.memory.store import merge_entry

    merged, replaced = merge_entry(["FIȘA\nold", "other"], "FIȘA\nnew")
    assert (merged, replaced) == (["FIȘA\nnew", "other"], True)


def test_a_first_line_over_sixty_characters_is_no_title():
    from surogates.memory.store import merge_entry

    long = "x" * 61
    merged, replaced = merge_entry([f"{long}\na"], f"{long}\nb")
    assert replaced is False and len(merged) == 2
