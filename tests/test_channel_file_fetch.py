import pytest

from surogates.channels.platforms.slack import SlackPlatform


class _FilesClient:
    def __init__(self, file_obj, *, raises=False):
        self._file = file_obj
        self._raises = raises

    async def files_info(self, file):
        if self._raises:
            raise RuntimeError("file_not_found")
        return {"file": self._file}


def _platform_with_files(client):
    p = SlackPlatform()
    p._get_client = lambda token: client  # type: ignore
    return p


async def test_fetch_file_meta_returns_file_object():
    fobj = {"id": "F1", "name": "a.pdf", "url_private_download": "https://slack.com/x",
            "mimetype": "application/pdf", "size": 10, "channels": ["C1"]}
    p = _platform_with_files(_FilesClient(fobj))
    out = await p.fetch_file_meta(creds={"bot_token": "xoxb"}, file_id="F1")
    assert out == fobj


async def test_fetch_file_meta_none_without_token():
    p = _platform_with_files(_FilesClient({"id": "F1"}))
    assert await p.fetch_file_meta(creds={}, file_id="F1") is None


async def test_fetch_file_meta_none_on_error():
    p = _platform_with_files(_FilesClient(None, raises=True))
    assert await p.fetch_file_meta(creds={"bot_token": "xoxb"}, file_id="F1") is None


from types import SimpleNamespace
from uuid import uuid4

from surogates.channels import file_fetch
from surogates.channels.file_fetch import (
    ChannelFileForbidden,
    ChannelFileNotFound,
    ChannelFileTooLarge,
    ChannelFileUnavailable,
    fetch_channel_file,
)


class _FakePlatform:
    """Injectable stand-in: descriptor.vault_refs + the two Slack calls."""

    def __init__(self, *, meta, data=b"bytes"):
        self._meta = meta
        self._data = data
        self.descriptor = SimpleNamespace(
            vault_refs=lambda identifier: {"bot_token": "bot_token"},
        )
        self.download_calls = 0

    async def fetch_file_meta(self, *, creds, file_id):
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
        session=_session(channel_id="C1"), bucket="b", file_id="F1")
    assert out["kind"] == "attachment"
    assert out["path"].startswith("uploads/slack/fetch/F1-")
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
        session=_session(channel_id="C1"), bucket="b", file_id="F1")
    assert "\n" not in out["path"]
    assert "\n" not in out["filename"]
    assert out["path"].startswith("uploads/slack/fetch/F1-")


async def test_fetch_channel_file_refuses_foreign_channel(monkeypatch):
    _patch_externals(monkeypatch)
    # File shared only in C2; the session is in C1 -> tenant isolation refuses.
    meta = {"name": "secret.html", "url_private_download": "https://slack.com/d",
            "mimetype": "text/html", "size": 12, "channels": ["C2"]}
    platform = _FakePlatform(meta=meta)
    with pytest.raises(ChannelFileForbidden):
        await fetch_channel_file(
            platform=platform, vault=object(), storage=object(),
            session=_session(channel_id="C1"), bucket="b", file_id="F1")
    assert platform.download_calls == 0  # never downloaded


async def test_fetch_channel_file_not_found(monkeypatch):
    _patch_externals(monkeypatch)
    platform = _FakePlatform(meta=None)
    with pytest.raises(ChannelFileNotFound):
        await fetch_channel_file(
            platform=platform, vault=object(), storage=object(),
            session=_session(), bucket="b", file_id="F1")


async def test_fetch_channel_file_too_large(monkeypatch):
    _patch_externals(monkeypatch)
    meta = {"name": "big.bin", "url_private_download": "https://slack.com/d",
            "mimetype": "application/octet-stream", "size": 99,
            "channels": ["C1"]}
    platform = _FakePlatform(meta=meta)
    with pytest.raises(ChannelFileTooLarge):
        await fetch_channel_file(
            platform=platform, vault=object(), storage=object(),
            session=_session(), bucket="b", file_id="F1", max_bytes=10)
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
            session=_session(), bucket="b", file_id="F1")
