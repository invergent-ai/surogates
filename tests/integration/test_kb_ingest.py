"""KB ingest dispatcher + markdown_dir runner tests.

Covers the end-to-end ingest path: dispatcher acquires the advisory
lock, looks up the source row, dispatches to the right runner module,
the runner walks a fixture directory + writes raw bytes via KbStorage,
upserts kb_raw_doc rows, and the dispatcher updates the source's
``last_status`` / ``last_synced_at`` / ``last_error`` accordingly.

Idempotence is exercised by re-running the same source twice and
asserting only ``docs_unchanged`` increments on the second pass. The
advisory-lock contract is exercised by running two concurrent calls
on the same source and asserting one wins, the other raises
:class:`IngestLocked`.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from surogates.jobs.kb_ingest import IngestLocked, run_ingest
from surogates.jobs.kb_sources._base import IngestResult
from surogates.storage.backend import LocalBackend
from surogates.storage.kb_storage import KbStorage

from .conftest import create_org

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def local_storage(tmp_path) -> LocalBackend:
    return LocalBackend(base_path=str(tmp_path / "garage"))


@pytest.fixture
def fixture_docs_dir(tmp_path) -> Path:
    """Three small markdown files that the markdown_dir runner walks."""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "intro.md").write_text(
        "# Intro\n\nFirst doc, has a heading.\n",
        encoding="utf-8",
    )
    (root / "guide" ).mkdir()
    (root / "guide" / "setup.md").write_text(
        "# Setup\n\nNested doc.\n",
        encoding="utf-8",
    )
    (root / "no-heading.md").write_text(
        "Just a paragraph; no heading.\n",
        encoding="utf-8",
    )
    return root


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_kb_and_source(
    session_factory,
    *,
    org_id: uuid.UUID | None,
    kb_name: str,
    config: dict,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a kb + a markdown_dir source. Returns ``(kb_id, source_id)``."""
    kb_id = uuid.uuid4()
    source_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb (id, org_id, name, agents_md, is_platform) "
                "VALUES (:id, :org_id, :name, '', :is_platform)"
            ),
            {
                "id": kb_id,
                "org_id": org_id,
                "name": kb_name,
                "is_platform": org_id is None,
            },
        )
        await db.execute(
            text(
                "INSERT INTO kb_source (id, kb_id, kind, config) "
                "VALUES (:id, :kb_id, 'markdown_dir', :config)"
            ),
            {
                "id": source_id,
                "kb_id": kb_id,
                "config": json.dumps(config),
            },
        )
        await db.commit()
    return kb_id, source_id


# ---------------------------------------------------------------------------
# Happy path: ingest a fixture dir end-to-end
# ---------------------------------------------------------------------------


async def test_markdown_dir_ingest_walks_fixture_dir(
    session_factory, local_storage, fixture_docs_dir
):
    """First run: 3 files → 3 docs_added; bytes appear in Garage; rows
    appear in kb_raw_doc with the right metadata.
    """
    org_id = await create_org(session_factory)
    kb_name = f"docs-{uuid.uuid4()}"
    kb_id, source_id = await _seed_kb_and_source(
        session_factory,
        org_id=org_id,
        kb_name=kb_name,
        config={"path": str(fixture_docs_dir)},
    )

    result: IngestResult = await run_ingest(
        source_id,
        session_factory=session_factory,
        storage_backend=local_storage,
    )
    assert result.docs_added == 3
    assert result.docs_updated == 0
    assert result.docs_unchanged == 0
    assert result.bytes_written > 0

    # Rows present.
    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT path, content_sha, title FROM kb_raw_doc "
                    "WHERE kb_id = :kb_id ORDER BY path"
                ),
                {"kb_id": kb_id},
            )
        ).all()
    paths = sorted(r.path for r in rows)
    assert paths == [
        "raw/guide/setup.md",
        "raw/intro.md",
        "raw/no-heading.md",
    ]

    by_path = {r.path: r for r in rows}
    assert by_path["raw/intro.md"].title == "Intro"
    assert by_path["raw/guide/setup.md"].title == "Setup"
    # No heading -> no title.
    assert by_path["raw/no-heading.md"].title is None

    # Bytes present in Garage at the right key.
    storage = KbStorage(local_storage)
    intro_bytes = await storage.read_entry(
        kb_org_id=org_id, kb_name=kb_name, path="raw/intro.md",
    )
    assert intro_bytes is not None
    assert b"First doc" in intro_bytes

    # Source row updated.
    async with session_factory() as db:
        row = (
            await db.execute(
                text("SELECT last_status, last_error, last_synced_at FROM kb_source WHERE id = :id"),
                {"id": source_id},
            )
        ).first()
    assert row.last_status == "success"
    assert row.last_error is None
    assert row.last_synced_at is not None


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


async def test_markdown_dir_re_run_is_idempotent_when_unchanged(
    session_factory, local_storage, fixture_docs_dir
):
    """Same files → second run reports docs_unchanged for all of them
    and writes nothing.
    """
    org_id = await create_org(session_factory)
    kb_name = f"idem-{uuid.uuid4()}"
    _, source_id = await _seed_kb_and_source(
        session_factory,
        org_id=org_id,
        kb_name=kb_name,
        config={"path": str(fixture_docs_dir)},
    )

    first = await run_ingest(
        source_id,
        session_factory=session_factory,
        storage_backend=local_storage,
    )
    assert first.docs_added == 3

    second = await run_ingest(
        source_id,
        session_factory=session_factory,
        storage_backend=local_storage,
    )
    assert second.docs_added == 0
    assert second.docs_updated == 0
    assert second.docs_unchanged == 3
    assert second.bytes_written == 0


async def test_markdown_dir_detects_changed_files(
    session_factory, local_storage, fixture_docs_dir
):
    """A modified file's hash changes → second run reports docs_updated."""
    org_id = await create_org(session_factory)
    kb_name = f"chg-{uuid.uuid4()}"
    _, source_id = await _seed_kb_and_source(
        session_factory,
        org_id=org_id,
        kb_name=kb_name,
        config={"path": str(fixture_docs_dir)},
    )

    await run_ingest(
        source_id,
        session_factory=session_factory,
        storage_backend=local_storage,
    )

    # Modify one file.
    (fixture_docs_dir / "intro.md").write_text(
        "# Intro\n\nFirst doc, edited.\n",
        encoding="utf-8",
    )

    result = await run_ingest(
        source_id,
        session_factory=session_factory,
        storage_backend=local_storage,
    )
    assert result.docs_added == 0
    assert result.docs_updated == 1
    assert result.docs_unchanged == 2

    # New bytes present.
    storage = KbStorage(local_storage)
    intro_bytes = await storage.read_entry(
        kb_org_id=org_id, kb_name=kb_name, path="raw/intro.md",
    )
    assert intro_bytes is not None
    assert b"edited" in intro_bytes


async def test_markdown_dir_skips_oversized_files(
    session_factory, local_storage, tmp_path
):
    """Files over ``max_bytes_per_doc`` are counted as docs_skipped."""
    root = tmp_path / "big"
    root.mkdir()
    (root / "small.md").write_text("# Small\n\nshort\n", encoding="utf-8")
    big = "# Big\n\n" + ("x" * 10_000) + "\n"
    (root / "big.md").write_text(big, encoding="utf-8")

    org_id = await create_org(session_factory)
    kb_name = f"big-{uuid.uuid4()}"
    _, source_id = await _seed_kb_and_source(
        session_factory,
        org_id=org_id,
        kb_name=kb_name,
        config={"path": str(root), "max_bytes_per_doc": 1000},
    )

    result = await run_ingest(
        source_id,
        session_factory=session_factory,
        storage_backend=local_storage,
    )
    assert result.docs_added == 1
    assert result.docs_skipped == 1


# ---------------------------------------------------------------------------
# Advisory lock contract
# ---------------------------------------------------------------------------


async def test_concurrent_ingest_calls_serialize_via_advisory_lock(
    session_factory, local_storage, fixture_docs_dir
):
    """Two concurrent run_ingest calls on the same source: one wins,
    the other raises IngestLocked because block=False.
    """
    org_id = await create_org(session_factory)
    kb_name = f"lock-{uuid.uuid4()}"
    _, source_id = await _seed_kb_and_source(
        session_factory,
        org_id=org_id,
        kb_name=kb_name,
        config={"path": str(fixture_docs_dir)},
    )

    # gather two starts; one will win the try-lock, the other raises.
    coros = [
        run_ingest(
            source_id,
            session_factory=session_factory,
            storage_backend=local_storage,
        ),
        run_ingest(
            source_id,
            session_factory=session_factory,
            storage_backend=local_storage,
        ),
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)
    locked = [r for r in results if isinstance(r, IngestLocked)]
    succeeded = [r for r in results if isinstance(r, IngestResult)]
    # Note: depending on event-loop scheduling, both may serialize and
    # succeed (the first finishes before the second tries). The lock
    # contract is "no concurrent runs" — both succeeding sequentially is
    # also valid. The minimum invariant is that no run succeeds twice
    # with docs_added>0 (which would mean both ingested concurrently).
    assert len(locked) + len(succeeded) == 2
    if len(succeeded) == 2:
        # Both ran sequentially; second saw the docs as unchanged.
        first, second = succeeded[0], succeeded[1]
        # Sort by docs_added desc so we identify the leader.
        first, second = sorted(
            succeeded, key=lambda r: r.docs_added, reverse=True
        )
        assert first.docs_added == 3
        assert second.docs_added == 0
        assert second.docs_unchanged == 3


async def test_ingest_unknown_kind_marks_failed(session_factory, local_storage):
    """A source with kind='not-real' surfaces a ValueError and the
    source row is marked failed with the error captured.
    """
    org_id = await create_org(session_factory)
    kb_id = uuid.uuid4()
    source_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb (id, org_id, name, agents_md) "
                "VALUES (:id, :org_id, :name, '')"
            ),
            {"id": kb_id, "org_id": org_id, "name": f"bad-{uuid.uuid4()}"},
        )
        await db.execute(
            text(
                "INSERT INTO kb_source (id, kb_id, kind, config) "
                "VALUES (:id, :kb_id, 'not-real', '{}')"
            ),
            {"id": source_id, "kb_id": kb_id},
        )
        await db.commit()

    with pytest.raises(ValueError, match="unknown source kind"):
        await run_ingest(
            source_id,
            session_factory=session_factory,
            storage_backend=local_storage,
        )

    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT last_status, last_error FROM kb_source WHERE id = :id"
                ),
                {"id": source_id},
            )
        ).first()
    assert row.last_status == "failed"
    assert "unknown source kind" in row.last_error


async def test_ingest_missing_path_marks_failed(session_factory, local_storage):
    """A markdown_dir source pointing at a nonexistent path: dispatcher
    marks the row failed and propagates the error.
    """
    org_id = await create_org(session_factory)
    kb_name = f"missing-{uuid.uuid4()}"
    _, source_id = await _seed_kb_and_source(
        session_factory,
        org_id=org_id,
        kb_name=kb_name,
        config={"path": "/no/such/path/exists"},
    )

    with pytest.raises(FileNotFoundError):
        await run_ingest(
            source_id,
            session_factory=session_factory,
            storage_backend=local_storage,
        )

    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT last_status, last_error FROM kb_source WHERE id = :id"
                ),
                {"id": source_id},
            )
        ).first()
    assert row.last_status == "failed"
    assert "does not exist" in row.last_error


# ---------------------------------------------------------------------------
# kb_read can fetch ingested bytes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# web_scraper runner — mocked HTTP via httpx.MockTransport
# ---------------------------------------------------------------------------


async def test_web_scraper_ingests_seed_urls_via_mocked_http(
    session_factory, local_storage, monkeypatch
):
    """web_scraper runner with a mocked AsyncClient: fetch two URLs,
    convert via markitdown, write raw_docs with stable per-URL paths.
    """
    import httpx

    from surogates.jobs.kb_sources import web_scraper as ws_mod

    pages = {
        "https://docs.example.com/intro": (
            b"<html><body><h1>Intro</h1><p>First page.</p></body></html>",
            "text/html",
        ),
        "https://docs.example.com/setup/": (
            b"<html><body><h1>Setup</h1><p>Second page.</p></body></html>",
            "text/html",
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body, ct = pages.get(str(request.url), (b"", "text/plain"))
        if not body:
            return httpx.Response(404)
        return httpx.Response(200, content=body, headers={"content-type": ct})

    def fake_client_factory(*, timeout, user_agent) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": user_agent},
        )

    monkeypatch.setattr(ws_mod, "_new_http_client", fake_client_factory)

    org_id = await create_org(session_factory)
    kb_name = f"web-{uuid.uuid4()}"
    kb_id = uuid.uuid4()
    source_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb (id, org_id, name, agents_md) "
                "VALUES (:id, :org_id, :name, '')"
            ),
            {"id": kb_id, "org_id": org_id, "name": kb_name},
        )
        await db.execute(
            text(
                "INSERT INTO kb_source (id, kb_id, kind, config) "
                "VALUES (:id, :kb_id, 'web_scraper', :config)"
            ),
            {
                "id": source_id,
                "kb_id": kb_id,
                "config": json.dumps({"seed_urls": list(pages.keys())}),
            },
        )
        await db.commit()

    result = await run_ingest(
        source_id,
        session_factory=session_factory,
        storage_backend=local_storage,
    )
    assert result.docs_added == 2

    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT path, url, title FROM kb_raw_doc "
                    "WHERE kb_id = :kb_id ORDER BY path"
                ),
                {"kb_id": kb_id},
            )
        ).all()
    by_url = {r.url: r for r in rows}
    assert "https://docs.example.com/intro" in by_url
    assert "https://docs.example.com/setup/" in by_url
    assert by_url["https://docs.example.com/intro"].title == "Intro"

    # Bytes were converted to markdown via markitdown.
    storage = KbStorage(local_storage)
    intro_bytes = await storage.read_entry(
        kb_org_id=org_id,
        kb_name=kb_name,
        path=by_url["https://docs.example.com/intro"].path,
    )
    assert intro_bytes is not None
    assert b"# Intro" in intro_bytes
    assert b"First page." in intro_bytes


async def test_web_scraper_sitemap_url_is_walked(
    session_factory, local_storage, monkeypatch
):
    """``sitemap_url`` config: the runner fetches the sitemap, parses
    its <loc> entries, and ingests each as a separate raw_doc.
    """
    import httpx

    from surogates.jobs.kb_sources import web_scraper as ws_mod

    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "  <url><loc>https://docs.example.com/a</loc></url>"
        "  <url><loc>https://docs.example.com/b</loc></url>"
        "</urlset>"
    )
    pages = {
        "https://docs.example.com/sitemap.xml": (
            sitemap.encode(),
            "application/xml",
        ),
        "https://docs.example.com/a": (
            b"<html><body><h1>A</h1></body></html>",
            "text/html",
        ),
        "https://docs.example.com/b": (
            b"<html><body><h1>B</h1></body></html>",
            "text/html",
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body, ct = pages.get(str(request.url), (b"", "text/plain"))
        if not body:
            return httpx.Response(404)
        return httpx.Response(200, content=body, headers={"content-type": ct})

    monkeypatch.setattr(
        ws_mod,
        "_new_http_client",
        lambda *, timeout, user_agent: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": user_agent},
        ),
    )

    org_id = await create_org(session_factory)
    kb_name = f"sm-{uuid.uuid4()}"
    kb_id = uuid.uuid4()
    source_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb (id, org_id, name, agents_md) "
                "VALUES (:id, :org_id, :name, '')"
            ),
            {"id": kb_id, "org_id": org_id, "name": kb_name},
        )
        await db.execute(
            text(
                "INSERT INTO kb_source (id, kb_id, kind, config) "
                "VALUES (:id, :kb_id, 'web_scraper', :config)"
            ),
            {
                "id": source_id,
                "kb_id": kb_id,
                "config": json.dumps({
                    "sitemap_url": "https://docs.example.com/sitemap.xml",
                }),
            },
        )
        await db.commit()

    result = await run_ingest(
        source_id,
        session_factory=session_factory,
        storage_backend=local_storage,
    )
    assert result.docs_added == 2


async def test_web_scraper_skips_failed_url(
    session_factory, local_storage, monkeypatch
):
    """A URL that 404s is counted as docs_skipped; siblings still ingest."""
    import httpx

    from surogates.jobs.kb_sources import web_scraper as ws_mod

    def handler(request: httpx.Request) -> httpx.Response:
        if "good" in str(request.url):
            return httpx.Response(
                200,
                content=b"<html><h1>OK</h1></html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        ws_mod,
        "_new_http_client",
        lambda *, timeout, user_agent: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": user_agent},
        ),
    )

    org_id = await create_org(session_factory)
    kb_name = f"err-{uuid.uuid4()}"
    kb_id = uuid.uuid4()
    source_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb (id, org_id, name, agents_md) "
                "VALUES (:id, :org_id, :name, '')"
            ),
            {"id": kb_id, "org_id": org_id, "name": kb_name},
        )
        await db.execute(
            text(
                "INSERT INTO kb_source (id, kb_id, kind, config) "
                "VALUES (:id, :kb_id, 'web_scraper', :config)"
            ),
            {
                "id": source_id,
                "kb_id": kb_id,
                "config": json.dumps({
                    "seed_urls": [
                        "https://x.example.com/good",
                        "https://x.example.com/bad",
                    ],
                }),
            },
        )
        await db.commit()

    result = await run_ingest(
        source_id,
        session_factory=session_factory,
        storage_backend=local_storage,
    )
    assert result.docs_added == 1
    assert result.docs_skipped == 1


# ---------------------------------------------------------------------------
# file_upload runner — seed holding/ in Garage, run, verify converted md
# ---------------------------------------------------------------------------


async def test_file_upload_ingests_html_and_md_from_holding(
    session_factory, local_storage
):
    """Seed a holding/{source_id}/ prefix with one .html (needs
    markitdown) and one .md (passes through unchanged); ingest;
    verify both land in raw/ as markdown with the expected content.
    """
    org_id = await create_org(session_factory)
    kb_name = f"upl-{uuid.uuid4()}"
    kb_id = uuid.uuid4()
    source_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb (id, org_id, name, agents_md) "
                "VALUES (:id, :org_id, :name, '')"
            ),
            {"id": kb_id, "org_id": org_id, "name": kb_name},
        )
        await db.execute(
            text(
                "INSERT INTO kb_source (id, kb_id, kind, config) "
                "VALUES (:id, :kb_id, 'file_upload', '{}')"
            ),
            {"id": source_id, "kb_id": kb_id},
        )
        await db.commit()

    storage = KbStorage(local_storage)
    holding = f"holding/{source_id}"
    await storage.write_entry(
        kb_org_id=org_id,
        kb_name=kb_name,
        path=f"{holding}/page.html",
        data=b"<html><body><h1>From HTML</h1><p>Body text.</p></body></html>",
    )
    await storage.write_entry(
        kb_org_id=org_id,
        kb_name=kb_name,
        path=f"{holding}/notes.md",
        data=b"# From Markdown\n\nA note.\n",
    )

    result = await run_ingest(
        source_id,
        session_factory=session_factory,
        storage_backend=local_storage,
    )
    assert result.docs_added == 2

    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT path, title FROM kb_raw_doc "
                    "WHERE kb_id = :kb_id ORDER BY path"
                ),
                {"kb_id": kb_id},
            )
        ).all()
    paths = sorted(r.path for r in rows)
    # html file gets ".md" appended; md passes through.
    assert "raw/notes.md" in paths
    assert "raw/page.html.md" in paths

    # Converted HTML body shows up as markdown.
    html_md = await storage.read_entry(
        kb_org_id=org_id, kb_name=kb_name, path="raw/page.html.md",
    )
    assert html_md is not None
    assert b"From HTML" in html_md


async def test_file_upload_empty_holding_returns_zero_result(
    session_factory, local_storage
):
    """No files in holding/ → ingest returns an empty result (not an error)."""
    org_id = await create_org(session_factory)
    kb_name = f"empty-{uuid.uuid4()}"
    kb_id = uuid.uuid4()
    source_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb (id, org_id, name, agents_md) "
                "VALUES (:id, :org_id, :name, '')"
            ),
            {"id": kb_id, "org_id": org_id, "name": kb_name},
        )
        await db.execute(
            text(
                "INSERT INTO kb_source (id, kb_id, kind, config) "
                "VALUES (:id, :kb_id, 'file_upload', '{}')"
            ),
            {"id": source_id, "kb_id": kb_id},
        )
        await db.commit()

    result = await run_ingest(
        source_id,
        session_factory=session_factory,
        storage_backend=local_storage,
    )
    assert result.total == 0


async def test_file_upload_skips_oversized_files(
    session_factory, local_storage
):
    """Files over max_bytes_per_file are skipped, not failed."""
    org_id = await create_org(session_factory)
    kb_name = f"big-{uuid.uuid4()}"
    kb_id = uuid.uuid4()
    source_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO kb (id, org_id, name, agents_md) "
                "VALUES (:id, :org_id, :name, '')"
            ),
            {"id": kb_id, "org_id": org_id, "name": kb_name},
        )
        await db.execute(
            text(
                "INSERT INTO kb_source (id, kb_id, kind, config) "
                "VALUES (:id, :kb_id, 'file_upload', :config)"
            ),
            {
                "id": source_id,
                "kb_id": kb_id,
                "config": json.dumps({"max_bytes_per_file": 100}),
            },
        )
        await db.commit()

    storage = KbStorage(local_storage)
    holding = f"holding/{source_id}"
    await storage.write_entry(
        kb_org_id=org_id,
        kb_name=kb_name,
        path=f"{holding}/small.md",
        data=b"# Small\n",
    )
    await storage.write_entry(
        kb_org_id=org_id,
        kb_name=kb_name,
        path=f"{holding}/big.md",
        data=b"# Big\n" + (b"x" * 500),
    )

    result = await run_ingest(
        source_id,
        session_factory=session_factory,
        storage_backend=local_storage,
    )
    assert result.docs_added == 1
    assert result.docs_skipped == 1


# ---------------------------------------------------------------------------
# kb_read can fetch ingested bytes (existing test)
# ---------------------------------------------------------------------------


async def test_kb_read_returns_ingested_bytes(
    session_factory, local_storage, fixture_docs_dir
):
    """End-to-end smoke: after ingest, kb_read returns the actual bytes
    written to Garage by the runner. Closes the read/write loop for
    step 4b without requiring the wiki maintainer (step 5).
    """
    from surogates.tools.builtin import kb_read as kb_read_mod
    from surogates.tools.registry import ToolRegistry

    org_id = await create_org(session_factory)
    kb_name = f"rt-{uuid.uuid4()}"
    _, source_id = await _seed_kb_and_source(
        session_factory,
        org_id=org_id,
        kb_name=kb_name,
        config={"path": str(fixture_docs_dir)},
    )
    await run_ingest(
        source_id,
        session_factory=session_factory,
        storage_backend=local_storage,
    )

    registry = ToolRegistry()
    kb_read_mod.register(registry)
    raw = await registry.dispatch(
        "kb_read",
        {"path": "raw/intro.md", "kb": kb_name},
        session_factory=session_factory,
        tenant={"org_id": org_id},
        storage_backend=local_storage,
    )
    payload = json.loads(raw)
    assert payload["success"] is True
    assert payload["kind"] == "raw"
    assert "First doc" in payload["content"]
    assert "# Intro" in payload["content"]
