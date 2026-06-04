"""Tests for the research evidence-bank pure logic.

The evidence bank is the planner's "what I have read and found credible"
record.  The writer later cites entries by their stable ``source_id``
(``S1``, ``S2``, ...).  Three invariants this test pins down:

* Source IDs are assigned sequentially and remain stable across calls.
* Adding a URL that already exists is a no-op — same ``MemoryEntry`` is
  returned so the planner can refer to the existing ``source_id``.
* JSONL round-trips losslessly (the tool persists to disk between
  parent and child sessions).
"""

from __future__ import annotations

from surogates.research.memory_bank import (
    MemoryEntry,
    add_entry,
    parse_jsonl,
    retrieve,
    serialize_jsonl,
)


def test_add_entry_assigns_sequential_source_ids() -> None:
    entries: list[MemoryEntry] = []

    e1 = add_entry(
        entries, url="https://a.test", title="A",
        summary="alpha", evidence=["x"],
    )
    e2 = add_entry(
        entries, url="https://b.test", title="B",
        summary="beta", evidence=["y"],
    )

    assert e1.source_id == "S1"
    assert e2.source_id == "S2"
    assert len(entries) == 2


def test_add_entry_dedupes_by_url() -> None:
    entries: list[MemoryEntry] = []

    first = add_entry(
        entries, url="https://a.test", title="A",
        summary="alpha", evidence=["x"],
    )
    again = add_entry(
        entries, url="https://a.test", title="A2",
        summary="alpha2", evidence=["z"],
    )

    assert again.source_id == first.source_id
    assert again is first
    assert len(entries) == 1


def test_add_entry_handles_no_evidence() -> None:
    """The planner may add a source whose summary is enough on its own.
    Evidence defaults to an empty list, not ``None``."""

    entries: list[MemoryEntry] = []
    e = add_entry(entries, url="u", title="t", summary="s")
    assert e.evidence == []


def test_roundtrip_jsonl_preserves_fields() -> None:
    entries: list[MemoryEntry] = []
    add_entry(
        entries, url="https://a.test", title="A",
        summary="alpha", evidence=["x", "y"],
    )

    text = serialize_jsonl(entries)
    parsed = parse_jsonl(text)

    assert len(parsed) == 1
    assert parsed[0].source_id == "S1"
    assert parsed[0].url == "https://a.test"
    assert parsed[0].title == "A"
    assert parsed[0].summary == "alpha"
    assert parsed[0].evidence == ["x", "y"]


def test_parse_jsonl_tolerates_blank_lines_and_garbage() -> None:
    """A partially-written file (worker crashed mid-write, manual
    edit, etc.) must not break the next session — skip junk, keep
    valid lines."""

    text = (
        '{"source_id":"S1","url":"u","title":"t","summary":"s","evidence":[]}\n'
        "\n"
        "not-json\n"
    )

    parsed = parse_jsonl(text)

    assert len(parsed) == 1
    assert parsed[0].source_id == "S1"


def test_parse_jsonl_coerces_missing_or_wrong_typed_fields() -> None:
    """Defensive parsing: anything that does not match the expected
    field shape is coerced to an empty/string value rather than
    raising.  A planner that hand-wrote an entry without ``evidence``
    still loads cleanly."""

    text = '{"source_id":"S5","url":"u","title":"t","summary":"s"}\n'

    parsed = parse_jsonl(text)

    assert parsed[0].source_id == "S5"
    assert parsed[0].evidence == []


def test_retrieve_ranks_by_keyword_overlap() -> None:
    entries: list[MemoryEntry] = []
    add_entry(
        entries, url="u1", title="Quantum computing basics",
        summary="qubits and superposition", evidence=["qubit"],
    )
    add_entry(
        entries, url="u2", title="Baking sourdough",
        summary="flour and starter", evidence=["bread"],
    )

    hits = retrieve(entries, query="qubit superposition", k=1)

    assert len(hits) == 1
    assert hits[0].url == "u1"


def test_retrieve_empty_query_returns_first_k() -> None:
    """An empty query is a legitimate 'give me everything you have'
    request used by the writer to enumerate the bank for the
    References section."""

    entries: list[MemoryEntry] = []
    for i in range(5):
        add_entry(entries, url=f"u{i}", title=f"t{i}", summary="s")

    hits = retrieve(entries, query="", k=3)

    assert [e.url for e in hits] == ["u0", "u1", "u2"]


def test_retrieve_k_caps_results() -> None:
    entries: list[MemoryEntry] = []
    for i in range(5):
        add_entry(
            entries, url=f"u{i}", title=f"topic {i}",
            summary="topic", evidence=[],
        )

    hits = retrieve(entries, query="topic", k=3)

    assert len(hits) == 3


def test_retrieve_ties_break_toward_earlier_entries() -> None:
    """When two entries score identically the older one (lower
    index) wins; this gives the writer a stable ordering across
    re-runs for the same outline section."""

    entries: list[MemoryEntry] = []
    for i in range(3):
        add_entry(entries, url=f"u{i}", title="alpha", summary="alpha")

    hits = retrieve(entries, query="alpha", k=2)

    assert [e.url for e in hits] == ["u0", "u1"]
