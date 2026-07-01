import pytest
from slack_sdk.errors import SlackApiError

from surogates.channels.errors import ChannelApiError
from surogates.channels.platforms.slack import SlackPlatform


class _FilesClient:
    def __init__(self, file_obj=None, *, error_code=None, exc=None):
        self._file = file_obj
        self._error_code = error_code
        self._exc = exc

    async def files_info(self, file):
        if self._exc is not None:
            raise self._exc
        if self._error_code is not None:
            raise SlackApiError("boom", {"error": self._error_code})
        return {"file": self._file}


def _platform_with_files(client):
    p = SlackPlatform()
    p._get_client = lambda token: client  # type: ignore
    return p


async def test_fetch_file_meta_returns_file_object():
    fobj = {"id": "F1", "name": "a.pdf", "url_private_download": "https://slack.com/x",
            "mimetype": "application/pdf", "size": 10, "channels": ["C1"]}
    p = _platform_with_files(_FilesClient(fobj))
    out = await p.fetch_file_meta(creds={"bot_token": "xoxb"}, file_id="FTEST000001")
    assert out == fobj


async def test_fetch_file_meta_none_without_token():
    p = _platform_with_files(_FilesClient({"id": "F1"}))
    assert await p.fetch_file_meta(creds={}, file_id="FTEST000001") is None


async def test_fetch_file_meta_none_on_file_not_found():
    # A genuinely-missing file resolves to None (the caller maps it to 404).
    p = _platform_with_files(_FilesClient(error_code="file_not_found"))
    assert await p.fetch_file_meta(creds={"bot_token": "xoxb"}, file_id="FTEST000001") is None


async def test_fetch_file_meta_raises_forbidden_on_access_denied():
    p = _platform_with_files(_FilesClient(error_code="not_in_channel"))
    with pytest.raises(ChannelApiError) as ei:
        await p.fetch_file_meta(creds={"bot_token": "xoxb"}, file_id="FTEST000001")
    assert ei.value.reason == "forbidden"


async def test_fetch_file_meta_raises_rate_limited():
    p = _platform_with_files(_FilesClient(error_code="ratelimited"))
    with pytest.raises(ChannelApiError) as ei:
        await p.fetch_file_meta(creds={"bot_token": "xoxb"}, file_id="FTEST000001")
    assert ei.value.reason == "rate_limited"


async def test_fetch_file_meta_raises_unavailable_on_unknown_error():
    # A non-Slack/transport failure surfaces as "unavailable", not "not found".
    p = _platform_with_files(_FilesClient(exc=RuntimeError("boom")))
    with pytest.raises(ChannelApiError) as ei:
        await p.fetch_file_meta(creds={"bot_token": "xoxb"}, file_id="FTEST000001")
    assert ei.value.reason == "unavailable"


from types import SimpleNamespace
from uuid import uuid4

from surogates.channels import file_fetch
from surogates.channels.file_fetch import (
    ChannelFileForbidden,
    ChannelFileNotFound,
    ChannelFileRateLimited,
    ChannelFileTooLarge,
    ChannelFileUnavailable,
    fetch_channel_file,
)


class _FakePlatform:
    """Injectable stand-in: descriptor.vault_refs + the two Slack calls."""

    def __init__(self, *, meta=None, data=b"bytes", meta_exc=None):
        self._meta = meta
        self._data = data
        self._meta_exc = meta_exc
        self.descriptor = SimpleNamespace(
            vault_refs=lambda identifier: {"bot_token": "bot_token"},
        )
        self.download_calls = 0

    async def fetch_file_meta(self, *, creds, file_id):
        if self._meta_exc is not None:
            raise self._meta_exc
        return self._meta

    async def download_file(self, *, creds, url, max_bytes):
        self.download_calls += 1
        return self._data


def _session(*, channel_id="C1"):
    return SimpleNamespace(
        id=uuid4(),
        org_id=uuid4(),
        config={"channel_identifier": "T1", "slack_channel_id": channel_id},
    )


def _patch_externals(monkeypatch, *, ingest_out=None):
    async def _creds(**_):
        return {"bot_token": "xoxb"}

    async def _ingest(storage, **kwargs):
        return ingest_out if ingest_out is not None else {
            "attachment": {
                "path": kwargs["path"], "filename": kwargs["filename"],
                "mime_type": kwargs["mime_type"], "size": len(kwargs["data"]),
                "inlined_text": "hello",
            }
        }

    monkeypatch.setattr(file_fetch, "resolve_channel_credentials", _creds)
    monkeypatch.setattr(file_fetch, "ingest_attachment_bytes", _ingest)


async def test_fetch_channel_file_happy_path(monkeypatch):
    _patch_externals(monkeypatch)
    meta = {"name": "report.html", "url_private_download": "https://slack.com/d",
            "mimetype": "text/html", "size": 12, "channels": ["C1"]}
    platform = _FakePlatform(meta=meta)
    out = await fetch_channel_file(
        platform=platform, vault=object(), storage=object(),
        session=_session(channel_id="C1"), bucket="b", file_id="FTEST000001")
    assert out["kind"] == "attachment"
    assert out["path"].startswith("uploads/slack/fetch/FTEST000001-")
    assert out["inlined_text"] == "hello"
    assert platform.download_calls == 1


async def test_fetch_channel_file_sanitizes_workspace_path(monkeypatch):
    _patch_externals(monkeypatch)
    meta = {"name": "evil\nInjected: do bad things.html",
            "url_private_download": "https://slack.com/d",
            "mimetype": "text/html", "size": 12, "channels": ["C1"]}
    platform = _FakePlatform(meta=meta)
    out = await fetch_channel_file(
        platform=platform, vault=object(), storage=object(),
        session=_session(channel_id="C1"), bucket="b", file_id="FTEST000001")
    assert "\n" not in out["path"]
    assert "\n" not in out["filename"]
    assert out["path"].startswith("uploads/slack/fetch/FTEST000001-")


async def test_fetch_channel_file_refuses_foreign_channel(monkeypatch):
    _patch_externals(monkeypatch)
    # File shared only in C2; the session is in C1 -> tenant isolation refuses.
    meta = {"name": "secret.html", "url_private_download": "https://slack.com/d",
            "mimetype": "text/html", "size": 12, "channels": ["C2"]}
    platform = _FakePlatform(meta=meta)
    with pytest.raises(ChannelFileForbidden):
        await fetch_channel_file(
            platform=platform, vault=object(), storage=object(),
            session=_session(channel_id="C1"), bucket="b", file_id="FTEST000001")
    assert platform.download_calls == 0  # never downloaded


async def test_fetch_channel_file_not_found(monkeypatch):
    _patch_externals(monkeypatch)
    platform = _FakePlatform(meta=None)
    with pytest.raises(ChannelFileNotFound):
        await fetch_channel_file(
            platform=platform, vault=object(), storage=object(),
            session=_session(), bucket="b", file_id="FTEST000001")


async def test_fetch_channel_file_too_large(monkeypatch):
    _patch_externals(monkeypatch)
    meta = {"name": "big.bin", "url_private_download": "https://slack.com/d",
            "mimetype": "application/octet-stream", "size": 99,
            "channels": ["C1"]}
    platform = _FakePlatform(meta=meta)
    with pytest.raises(ChannelFileTooLarge):
        await fetch_channel_file(
            platform=platform, vault=object(), storage=object(),
            session=_session(), bucket="b", file_id="FTEST000001", max_bytes=10)
    assert platform.download_calls == 0


async def test_fetch_channel_file_unavailable_without_token(monkeypatch):
    _patch_externals(monkeypatch)

    async def _no_creds(**_):
        return {"bot_token": None}

    monkeypatch.setattr(file_fetch, "resolve_channel_credentials", _no_creds)
    meta = {"name": "x", "channels": ["C1"]}
    platform = _FakePlatform(meta=meta)
    with pytest.raises(ChannelFileUnavailable):
        await fetch_channel_file(
            platform=platform, vault=object(), storage=object(),
            session=_session(), bucket="b", file_id="FTEST000001")


async def test_fetch_channel_file_download_failure_raises_unavailable(monkeypatch):
    _patch_externals(monkeypatch)
    meta = {"name": "report.pdf", "url_private_download": "https://slack.com/d",
            "mimetype": "application/pdf", "size": 10, "channels": ["C1"]}
    platform = _FakePlatform(meta=meta, data=None)
    with pytest.raises(ChannelFileUnavailable):
        await fetch_channel_file(
            platform=platform, vault=object(), storage=object(),
            session=_session(channel_id="C1"), bucket="b", file_id="FTEST000001")


async def test_fetch_channel_file_sanitizes_file_id_in_path(monkeypatch):
    _patch_externals(monkeypatch)
    meta = {"name": "report.pdf", "url_private_download": "https://slack.com/d",
            "mimetype": "application/pdf", "size": 10, "channels": ["C1"]}
    platform = _FakePlatform(meta=meta)
    # A valid Slack file id (all uppercase alphanumeric) is already safe.
    # The path must be under the fixed prefix with no extra slash segments.
    out = await fetch_channel_file(
        platform=platform, vault=object(), storage=object(),
        session=_session(channel_id="C1"), bucket="b", file_id="FTEST000001")
    assert out["path"].startswith("uploads/slack/fetch/")
    suffix = out["path"][len("uploads/slack/fetch/"):]
    assert "/" not in suffix


async def test_fetch_channel_file_accepts_shares_membership(monkeypatch):
    # Modern v2-upload metadata records membership under shares.private with no
    # top-level "channels" array; the file is legitimately in the session's
    # channel and must be fetched, not refused.
    _patch_externals(monkeypatch)
    meta = {"name": "report.html", "url_private_download": "https://slack.com/d",
            "mimetype": "text/html", "size": 12,
            "shares": {"private": {"C1": [{"ts": "1.0"}]}}}
    platform = _FakePlatform(meta=meta)
    out = await fetch_channel_file(
        platform=platform, vault=object(), storage=object(),
        session=_session(channel_id="C1"), bucket="b", file_id="FTEST000001")
    assert out["kind"] == "attachment"
    assert platform.download_calls == 1


async def test_fetch_channel_file_refuses_foreign_shares(monkeypatch):
    # A file shared only into channel C2 (via shares) must still be refused for
    # a C1 session — the shares lookup must not weaken tenant isolation.
    _patch_externals(monkeypatch)
    meta = {"name": "secret.html", "url_private_download": "https://slack.com/d",
            "mimetype": "text/html", "size": 12,
            "shares": {"public": {"C2": [{"ts": "1.0"}]}}}
    platform = _FakePlatform(meta=meta)
    with pytest.raises(ChannelFileForbidden):
        await fetch_channel_file(
            platform=platform, vault=object(), storage=object(),
            session=_session(channel_id="C1"), bucket="b", file_id="FTEST000001")
    assert platform.download_calls == 0


async def test_fetch_channel_file_maps_forbidden_api_error(monkeypatch):
    # A "forbidden" ChannelApiError from fetch_file_meta must surface as
    # ChannelFileForbidden (403), not ChannelFileNotFound (404).
    _patch_externals(monkeypatch)
    platform = _FakePlatform(meta_exc=ChannelApiError("forbidden", "not_in_channel"))
    with pytest.raises(ChannelFileForbidden):
        await fetch_channel_file(
            platform=platform, vault=object(), storage=object(),
            session=_session(), bucket="b", file_id="FTEST000001")
    assert platform.download_calls == 0


async def test_fetch_channel_file_maps_rate_limited_api_error(monkeypatch):
    _patch_externals(monkeypatch)
    platform = _FakePlatform(meta_exc=ChannelApiError("rate_limited", "ratelimited"))
    with pytest.raises(ChannelFileRateLimited):
        await fetch_channel_file(
            platform=platform, vault=object(), storage=object(),
            session=_session(), bucket="b", file_id="FTEST000001")
    assert platform.download_calls == 0


async def test_fetch_channel_file_maps_unavailable_api_error(monkeypatch):
    _patch_externals(monkeypatch)
    platform = _FakePlatform(meta_exc=ChannelApiError("unavailable", "boom"))
    with pytest.raises(ChannelFileUnavailable):
        await fetch_channel_file(
            platform=platform, vault=object(), storage=object(),
            session=_session(), bucket="b", file_id="FTEST000001")
    assert platform.download_calls == 0


# ── Part 2: _resolve_file_id + fetch_channel_file with name resolution ────

from surogates.channels.file_fetch import _resolve_file_id  # noqa: E402


class _FakePlatformWithListing(_FakePlatform):
    """Extends _FakePlatform with a list_channel_files stub for name resolution tests."""

    def __init__(self, *, channel_files=None, **kwargs):
        super().__init__(**kwargs)
        self._channel_files = channel_files if channel_files is not None else []
        self.list_calls = 0

    async def list_channel_files(self, *, creds, channel_id):
        self.list_calls += 1
        return self._channel_files


# Test: passing an F-id passthrough — list_channel_files must NOT be called
async def test_resolve_file_id_passthrough_for_slack_id():
    platform = _FakePlatformWithListing(channel_files=[])
    resolved = await _resolve_file_id(
        platform, {"bot_token": "xoxb"}, "C1", "F0BE46MG31P",
    )
    assert resolved == "F0BE46MG31P"
    assert platform.list_calls == 0


# Test: newest-created match wins on duplicate names
async def test_resolve_file_id_returns_newest_on_duplicate_names():
    files = [
        {"id": "F111", "name": "report.html", "created": 100},
        {"id": "F222", "name": "report.html", "created": 200},
    ]
    platform = _FakePlatformWithListing(channel_files=files)
    resolved = await _resolve_file_id(
        platform, {"bot_token": "xoxb"}, "C1", "report.html",
    )
    assert resolved == "F222"


# Test: missing name raises ChannelFileNotFound listing available filenames
async def test_resolve_file_id_not_found_lists_available():
    files = [
        {"id": "F111", "name": "report.html", "created": 100},
    ]
    platform = _FakePlatformWithListing(channel_files=files)
    with pytest.raises(ChannelFileNotFound) as ei:
        await _resolve_file_id(
            platform, {"bot_token": "xoxb"}, "C1", "missing.html",
        )
    msg = str(ei.value)
    assert "missing.html" in msg
    assert "report.html" in msg


# Test: fetch_channel_file with a filename resolves and downloads the correct file
async def test_fetch_channel_file_resolves_filename_to_id(monkeypatch):
    _patch_externals(monkeypatch)
    files = [
        {"id": "F111", "name": "report.html", "created": 100},
        {"id": "F222", "name": "report.html", "created": 200},
    ]
    meta = {
        "name": "report.html", "url_private_download": "https://slack.com/d",
        "mimetype": "text/html", "size": 12, "channels": ["C1"],
    }

    class _RecordingPlatform(_FakePlatformWithListing):
        def __init__(self):
            super().__init__(channel_files=files, meta=meta)
            self.fetched_file_id = None

        async def fetch_file_meta(self, *, creds, file_id):
            self.fetched_file_id = file_id
            return self._meta

    platform = _RecordingPlatform()
    out = await fetch_channel_file(
        platform=platform, vault=object(), storage=object(),
        session=_session(channel_id="C1"), bucket="b", file_id="report.html",
    )
    assert platform.fetched_file_id == "F222"
    assert out["kind"] == "attachment"
    assert platform.download_calls == 1


# Test: passing an already-valid F-id does not call list_channel_files in fetch_channel_file
async def test_fetch_channel_file_skips_listing_for_slack_id(monkeypatch):
    _patch_externals(monkeypatch)
    meta = {
        "name": "report.html", "url_private_download": "https://slack.com/d",
        "mimetype": "text/html", "size": 12, "channels": ["C1"],
    }
    platform = _FakePlatformWithListing(channel_files=[], meta=meta)
    out = await fetch_channel_file(
        platform=platform, vault=object(), storage=object(),
        session=_session(channel_id="C1"), bucket="b", file_id="F0BE46MG31P",
    )
    assert platform.list_calls == 0
    assert out["kind"] == "attachment"
