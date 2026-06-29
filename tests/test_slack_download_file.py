from surogates.channels.platforms.slack import SlackPlatform


class _Resp:
    def __init__(self, status=200, headers=None, body=b"data"):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
    def raise_for_status(self):  # not used by impl, present for parity
        pass
    @property
    def content(self):
        return self._body


class _Client:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []
    async def get(self, url, headers=None):
        self.calls.append((url, headers))
        return self._resp
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def _platform_with_httpx(monkeypatch, resp):
    client = _Client(resp)
    import surogates.channels.platforms.slack as slk
    monkeypatch.setattr(slk.httpx, "AsyncClient", lambda *a, **k: client)
    return SlackPlatform(), client


async def test_download_sends_bearer_and_returns_bytes(monkeypatch):
    p, client = _platform_with_httpx(monkeypatch, _Resp(body=b"PDFBYTES"))
    out = await p.download_file(creds={"bot_token": "xoxb-1"}, url="https://files.slack.com/x", max_bytes=10_000)
    assert out == b"PDFBYTES"
    assert client.calls[0][1]["Authorization"] == "Bearer xoxb-1"


async def test_download_none_on_non_2xx(monkeypatch):
    p, _ = _platform_with_httpx(monkeypatch, _Resp(status=403))
    assert await p.download_file(creds={"bot_token": "x"}, url="u", max_bytes=10_000) is None


async def test_download_none_when_content_length_over_cap(monkeypatch):
    p, _ = _platform_with_httpx(monkeypatch, _Resp(headers={"Content-Length": "999999"}))
    assert await p.download_file(creds={"bot_token": "x"}, url="u", max_bytes=1000) is None


async def test_download_none_when_body_over_cap(monkeypatch):
    p, _ = _platform_with_httpx(monkeypatch, _Resp(body=b"x" * 2000))
    assert await p.download_file(creds={"bot_token": "x"}, url="u", max_bytes=1000) is None


async def test_download_none_when_no_token(monkeypatch):
    p, _ = _platform_with_httpx(monkeypatch, _Resp())
    assert await p.download_file(creds={}, url="u", max_bytes=1000) is None


async def test_download_refuses_non_slack_host_and_never_calls_http(monkeypatch):
    """Bot token must NEVER be sent to a non-Slack host."""
    p, client = _platform_with_httpx(monkeypatch, _Resp(body=b"STOLEN"))
    result = await p.download_file(
        creds={"bot_token": "xoxb-secret"},
        url="https://attacker.example.com/steal",
        max_bytes=10_000,
    )
    assert result is None
    assert client.calls == [], "client.get must not be called for non-Slack host"


async def test_download_allows_files_slack_com_host(monkeypatch):
    """files.slack.com is a valid Slack subdomain and must be allowed."""
    p, client = _platform_with_httpx(monkeypatch, _Resp(body=b"FILEDATA"))
    result = await p.download_file(
        creds={"bot_token": "xoxb-1"},
        url="https://files.slack.com/files-pri/abc/download",
        max_bytes=10_000,
    )
    assert result == b"FILEDATA"
    assert len(client.calls) == 1


async def test_download_refuses_slack_com_lookalike(monkeypatch):
    """slack.com.evil.org is NOT a slack host — must be refused."""
    p, client = _platform_with_httpx(monkeypatch, _Resp(body=b"STOLEN"))
    result = await p.download_file(
        creds={"bot_token": "xoxb-secret"},
        url="https://slack.com.evil.org/steal",
        max_bytes=10_000,
    )
    assert result is None
    assert client.calls == []
