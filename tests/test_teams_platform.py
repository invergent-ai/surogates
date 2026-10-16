"""Microsoft Teams platform: Bot Framework JWT verify, parse, send and typing."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from surogates.channels.channel_media import OutboundFile
from surogates.channels.delivery import is_permanent_delivery_error
from surogates.channels.platforms import teams as teams_mod
from surogates.channels.platforms.teams import (
    BOT_FRAMEWORK_ISSUER,
    TeamsPlatform,
    conversation_ref,
    parse,
)

APP_ID = "00000000-0000-4000-8000-00000000fea5"
TENANT = "00000000-0000-4000-8000-0000000000aa"
SECRET = "fake-teams-secret-0123456789"
SERVICE_URL = "https://smba.example.test/emea/"
OPENID_URL = "https://login.example.test/v1/.well-known/openidconfiguration"
JWKS_URL = "https://login.example.test/v1/.well-known/keys"
LOGIN_BASE = "https://login.microsoftonline.example.test"
BOT_ID = f"28:{APP_ID}"
USER_AAD = "6a1f0c1e-0000-4000-8000-000000000001"


def _rsa_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


KEY_A, KEY_B = _rsa_pem(), _rsa_pem()


def _jwk(pem: bytes, kid: str) -> dict:
    public = jwk.construct(pem, "RS256").public_key().to_dict()
    return {**public, "kid": kid, "use": "sig", "endorsements": ["msteams"]}


def _token(*, pem: bytes = KEY_A, kid: str = "A", **claims) -> str:
    now = int(time.time())
    body = {
        "iss": BOT_FRAMEWORK_ISSUER, "aud": APP_ID, "nbf": now - 10,
        "exp": now + 3600, "serviceurl": SERVICE_URL,
    }
    body.update(claims)
    return jwt.encode(body, pem.decode(), algorithm="RS256", headers={"kid": kid})


def _activity(**overrides) -> dict:
    activity = {
        "type": "message",
        "id": "1700000000001",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": "29:user-one", "name": "Ana Pop", "aadObjectId": USER_AAD},
        "recipient": {"id": BOT_ID, "name": "Desk"},
        "conversation": {"id": "a:personal-1", "conversationType": "personal", "tenantId": TENANT},
        "channelData": {"tenant": {"id": TENANT}},
        "text": "hello there",
    }
    activity.update(overrides)
    return activity


def _request(token: str | None) -> SimpleNamespace:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(headers=headers, path_params={"app_id": APP_ID})


@pytest.fixture
def platform(monkeypatch):
    monkeypatch.setattr(teams_mod, "TEAMS_OPENID_URL", OPENID_URL)
    monkeypatch.setattr(teams_mod, "TEAMS_LOGIN_BASE", LOGIN_BASE)
    return TeamsPlatform()


def _mock_keys(router, *keys: dict):
    router.get(OPENID_URL).mock(return_value=httpx.Response(200, json={"jwks_uri": JWKS_URL}))
    return router.get(JWKS_URL).mock(return_value=httpx.Response(200, json={"keys": list(keys)}))


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


async def _verify(platform, token, activity=None) -> bool:
    raw = json.dumps(activity or _activity()).encode()
    return await platform.verify(_request(token), raw, creds={})


@respx.mock
async def test_verify_accepts_a_bot_framework_token_for_this_app(platform):
    _mock_keys(respx, _jwk(KEY_A, "A"))
    assert await _verify(platform, _token()) is True


@respx.mock
@pytest.mark.parametrize("claims", [
    {"aud": "11111111-0000-4000-8000-000000000000"},
    {"iss": "https://sts.windows.net/evil/"},
    {"exp": int(time.time()) - 600, "nbf": int(time.time()) - 4000},
    {"serviceurl": "https://attacker.example.test/"},
])
async def test_verify_rejects_a_bad_claim(platform, claims):
    _mock_keys(respx, _jwk(KEY_A, "A"))
    assert await _verify(platform, _token(**claims)) is False


@respx.mock
async def test_verify_accepts_the_camel_case_service_url_claim(platform):
    _mock_keys(respx, _jwk(KEY_A, "A"))
    now = int(time.time())
    token = jwt.encode(
        {"iss": BOT_FRAMEWORK_ISSUER, "aud": APP_ID, "nbf": now - 10,
         "exp": now + 3600, "serviceUrl": SERVICE_URL},
        KEY_A.decode(), algorithm="RS256", headers={"kid": "A"},
    )
    assert await _verify(platform, token) is True


@respx.mock
async def test_verify_allows_five_minutes_of_clock_skew(platform):
    _mock_keys(respx, _jwk(KEY_A, "A"))
    assert await _verify(platform, _token(exp=int(time.time()) - 120)) is True


@respx.mock
async def test_verify_rejects_a_signature_from_another_key(platform):
    _mock_keys(respx, _jwk(KEY_A, "A"))
    assert await _verify(platform, _token(pem=KEY_B, kid="A")) is False


async def test_verify_rejects_a_missing_or_garbage_bearer(platform):
    assert await _verify(platform, None) is False
    assert await _verify(platform, "not-a-jwt") is False


@respx.mock
async def test_verify_rejects_a_key_not_endorsed_for_the_channel(platform):
    _mock_keys(respx, _jwk(KEY_A, "A"))
    assert await _verify(platform, _token(), _activity(channelId="skype")) is False


@respx.mock
async def test_an_unknown_kid_refetches_the_keys_once(platform, monkeypatch):
    monkeypatch.setattr(teams_mod, "_KID_REFETCH_S", 0)
    keys = _mock_keys(respx, _jwk(KEY_A, "A"))
    assert await _verify(platform, _token()) is True
    # The keys rotate: B appears.
    keys.mock(return_value=httpx.Response(200, json={"keys": [_jwk(KEY_A, "A"), _jwk(KEY_B, "B")]}))
    assert await _verify(platform, _token(pem=KEY_B, kid="B")) is True
    # Cached now — no further fetch for either kid.
    calls = keys.call_count
    assert await _verify(platform, _token(pem=KEY_B, kid="B")) is True
    assert keys.call_count == calls
    # A kid that never appears is rejected after one refetch.
    assert await _verify(platform, _token(kid="Z")) is False
    assert keys.call_count == calls + 1


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def test_parse_a_personal_message():
    msg = parse(_activity(), app_id=APP_ID)
    assert msg.is_dm and msg.visibility == "dm"
    assert msg.identifier == "a:personal-1" and msg.thread_key is None
    assert msg.platform_user_id == USER_AAD and msg.user_name == "Ana Pop"
    assert msg.text == "hello there" and msg.ts == "teams:1700000000001"
    assert msg.source["teams_service_url"] == SERVICE_URL
    assert msg.source["teams_tenant_id"] == TENANT
    assert msg.source["teams_conversation_id"] == "a:personal-1"
    assert msg.source["app_id"] == APP_ID


def test_parse_falls_back_to_the_teams_id_without_an_aad_object():
    msg = parse(_activity(**{"from": {"id": "29:guest", "name": "Guest"}}), app_id=APP_ID)
    assert msg.platform_user_id == "29:guest"


def test_parse_a_channel_mention_strips_the_bot_and_threads_on_the_root():
    activity = _activity(
        conversation={
            "id": "19:general@thread.tacv2;messageid=1699999999999",
            "conversationType": "channel", "tenantId": TENANT,
        },
        text="<at>Desk</at> what about <at>Ion Pop</at>?",
        entities=[
            {"type": "mention", "text": "<at>Desk</at>", "mentioned": {"id": BOT_ID, "name": "Desk"}},
            {"type": "mention", "text": "<at>Ion Pop</at>", "mentioned": {"id": "29:ion", "name": "Ion Pop"}},
        ],
    )
    msg = parse(activity, app_id=APP_ID)
    assert not msg.is_dm and msg.is_mention and msg.visibility == "private"
    assert msg.identifier == "19:general@thread.tacv2"
    assert msg.thread_key == "1699999999999"
    assert msg.text == "what about Ion Pop?"


def test_parse_a_group_chat_without_a_mention_is_not_a_mention():
    activity = _activity(
        conversation={"id": "19:chat@thread.v2", "conversationType": "groupChat"},
        entities=[{"type": "mention", "text": "<at>Ion</at>", "mentioned": {"id": "29:ion"}}],
        text="<at>Ion</at> lunch?",
    )
    msg = parse(activity, app_id=APP_ID)
    assert not msg.is_dm and not msg.is_mention
    assert msg.identifier == "19:chat@thread.v2" and msg.thread_key is None
    assert msg.text == "Ion lunch?"


def test_parse_drops_own_messages_and_non_messages():
    assert parse(_activity(**{"from": {"id": BOT_ID, "name": "Desk"}}), app_id=APP_ID) is None
    assert parse(_activity(type="conversationUpdate"), app_id=APP_ID) is None
    assert parse(_activity(type="messageReaction"), app_id=APP_ID) is None
    assert parse(_activity(text="", attachments=[]), app_id=APP_ID) is None


def test_parse_attachments_with_a_download_url_become_file_refs():
    activity = _activity(text="", attachments=[
        {
            "contentType": "application/vnd.microsoft.teams.file.download.info",
            "name": "report.pdf",
            "content": {"downloadUrl": "https://sp.example.test/dl/1", "fileType": "pdf"},
        },
        {"contentType": "text/html", "content": "<p>hi</p>"},
    ])
    msg = parse(activity, app_id=APP_ID)
    assert msg.kind == "document"
    [ref] = msg.files
    assert ref.url == "https://sp.example.test/dl/1"
    assert ref.filename == "report.pdf" and ref.mime_type == "application/pdf"


# ---------------------------------------------------------------------------
# conversationUpdate / conversation references
# ---------------------------------------------------------------------------


class _Redis:
    def __init__(self):
        self.data: dict = {}
        self.ttl: dict = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.ttl[key] = ex

    async def get(self, key):
        return self.data.get(key)


async def test_conversation_update_records_the_reference_and_is_handled(platform):
    redis = _Redis()
    install = _activity(type="conversationUpdate", text=None, membersAdded=[{"id": BOT_ID}])
    handled = await platform.handle_non_message_update(
        install, routing=SimpleNamespace(identifier=APP_ID), creds={},
        deps=SimpleNamespace(redis=redis),
    )
    assert handled is True
    ref = await conversation_ref(redis, APP_ID, user=USER_AAD)
    assert ref == {
        "conversation_id": "a:personal-1", "service_url": SERVICE_URL,
        "tenant_id": TENANT, "conversation_type": "personal",
    }
    assert await conversation_ref(redis, APP_ID, conversation_id="a:personal-1") == ref
    assert set(redis.ttl.values()) == {90 * 24 * 3600}


async def test_a_message_refreshes_the_reference_and_falls_through(platform):
    redis = _Redis()
    channel = _activity(conversation={
        "id": "19:general@thread.tacv2;messageid=42", "conversationType": "channel",
    })
    handled = await platform.handle_non_message_update(
        channel, routing=SimpleNamespace(identifier=APP_ID), creds={},
        deps=SimpleNamespace(redis=redis),
    )
    assert handled is False
    ref = await conversation_ref(redis, APP_ID, conversation_id="19:general@thread.tacv2")
    assert ref["service_url"] == SERVICE_URL
    # A channel is nobody's personal conversation.
    assert await conversation_ref(redis, APP_ID, user=USER_AAD) is None


async def test_other_activity_types_are_acknowledged_and_dropped(platform):
    handled = await platform.handle_non_message_update(
        _activity(type="installationUpdate"), routing=SimpleNamespace(identifier=APP_ID),
        creds={}, deps=SimpleNamespace(redis=None),
    )
    assert handled is True


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

TOKEN_URL = f"{LOGIN_BASE}/{TENANT}/oauth2/v2.0/token"
ACTIVITIES = f"{SERVICE_URL}v3/conversations/a%3Apersonal-1/activities"


def _creds() -> dict:
    return {"app_password": SECRET, "tenant_id": TENANT}


def _item(content: str = "Hi", **destination) -> SimpleNamespace:
    dest = {
        "conversation_id": "a:personal-1", "service_url": SERVICE_URL,
        "reply_to_id": None, "channel_identifier": APP_ID,
    }
    dest.update(destination)
    return SimpleNamespace(id=1, session_id="s", destination=dest, payload={"content": content})


def _mock_token(router, **json_body):
    body = {"access_token": "bf-token", "expires_in": 3600, "token_type": "Bearer"}
    body.update(json_body)
    return router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=body))


@respx.mock
async def test_send_posts_markdown_with_a_cached_client_credentials_token(platform):
    token = _mock_token(respx)
    post = respx.post(ACTIVITIES).mock(return_value=httpx.Response(201, json={"id": "out-1"}))
    first = await platform.send(_item("**hi**"), creds=_creds())
    second = await platform.send(_item("again"), creds=_creds())
    assert first.success and first.message_id == "out-1" and second.success
    assert token.call_count == 1
    form = dict(httpx.QueryParams(token.calls[0].request.content.decode()))
    assert form == {
        "grant_type": "client_credentials", "client_id": APP_ID,
        "client_secret": SECRET, "scope": "https://api.botframework.com/.default",
    }
    sent = json.loads(post.calls[0].request.content)
    assert sent == {"type": "message", "text": "**hi**", "textFormat": "markdown"}
    assert post.calls[0].request.headers["Authorization"] == "Bearer bf-token"


@respx.mock
async def test_an_expiring_token_is_fetched_again(platform):
    token = _mock_token(respx, expires_in=30)  # under the 60 s margin
    respx.post(ACTIVITIES).mock(return_value=httpx.Response(201, json={"id": "x"}))
    await platform.send(_item(), creds=_creds())
    await platform.send(_item(), creds=_creds())
    assert token.call_count == 2


@respx.mock
async def test_send_replies_into_a_channel_thread(platform):
    _mock_token(respx)
    url = f"{SERVICE_URL}v3/conversations/19%3Ag%40thread.tacv2%3Bmessageid%3D42/activities"
    post = respx.post(url).mock(return_value=httpx.Response(201, json={"id": "r"}))
    result = await platform.send(
        _item("ok", conversation_id="19:g@thread.tacv2", reply_to_id="42"), creds=_creds(),
    )
    assert result.success
    assert json.loads(post.calls[0].request.content)["replyToId"] == "42"


@respx.mock
async def test_send_splits_long_replies(platform):
    _mock_token(respx)
    post = respx.post(ACTIVITIES).mock(return_value=httpx.Response(201, json={"id": "c"}))
    text = ("word " * 6000).strip()  # ~30 000 chars
    result = await platform.send(_item(text), creds=_creds())
    assert result.success and post.call_count == 2
    assert all(len(json.loads(c.request.content)["text"]) <= 24000 for c in post.calls)


@respx.mock
async def test_send_edits_the_progress_message_in_place(platform):
    _mock_token(respx)
    put = respx.put(f"{ACTIVITIES}/act-9").mock(return_value=httpx.Response(200, json={"id": "act-9"}))
    result = await platform.send(_item("done", update_ts="act-9"), creds=_creds())
    assert result.success and result.message_id == "act-9"
    assert json.loads(put.calls[0].request.content) == {
        "type": "message", "id": "act-9", "text": "done", "textFormat": "markdown",
    }


@respx.mock
async def test_a_failed_edit_posts_fresh(platform):
    _mock_token(respx)
    respx.put(f"{ACTIVITIES}/act-9").mock(return_value=httpx.Response(404, json={}))
    post = respx.post(ACTIVITIES).mock(return_value=httpx.Response(201, json={"id": "new"}))
    result = await platform.send(_item("done", update_ts="act-9"), creds=_creds())
    assert result.success and result.message_id == "new" and post.call_count == 1


@respx.mock
@pytest.mark.parametrize("status,code", [
    (403, "MessageWritesBlocked"),
    (403, "ConversationBlockedByUser"),
    (403, "BotNotInConversationRoster"),
    (404, "NotFound"),
    (413, "MessageSizeTooBig"),
])
async def test_permanent_errors(platform, status, code):
    _mock_token(respx)
    respx.post(ACTIVITIES).mock(return_value=httpx.Response(
        status, json={"error": {"code": code, "message": "nope"}},
    ))
    result = await platform.send(_item(), creds=_creds())
    assert not result.success and is_permanent_delivery_error(result.error)


@respx.mock
async def test_throttling_and_server_errors_are_retryable(platform):
    _mock_token(respx)
    route = respx.post(ACTIVITIES).mock(return_value=httpx.Response(
        429, headers={"Retry-After": "2"}, json={"error": {"code": "Throttled", "message": "slow"}},
    ))
    result = await platform.send(_item(), creds=_creds())
    assert not result.success and not is_permanent_delivery_error(result.error)
    route.mock(return_value=httpx.Response(502, text="bad gateway"))
    result = await platform.send(_item(), creds=_creds())
    assert not result.success and not is_permanent_delivery_error(result.error)


@respx.mock
async def test_a_rejected_client_secret_is_permanent(platform):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={
        "error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret",
    }))
    result = await platform.send(_item(), creds=_creds())
    assert not result.success and is_permanent_delivery_error(result.error)


async def test_a_destination_without_a_reference_is_permanent(platform):
    result = await platform.send(_item(service_url=""), creds=_creds())
    assert not result.success and is_permanent_delivery_error(result.error)


@respx.mock
async def test_send_files_inlines_small_images_and_skips_the_rest(platform):
    _mock_token(respx)
    post = respx.post(ACTIVITIES).mock(return_value=httpx.Response(201, json={"id": "img"}))
    files = [
        OutboundFile(filename="chart.png", mime_type="image/png", data=b"\x89PNG"),
        OutboundFile(filename="big.png", mime_type="image/png", data=b"0" * (1024 * 1024 + 1)),
        OutboundFile(filename="report.pdf", mime_type="application/pdf", data=b"%PDF"),
    ]
    uploaded = await platform.send_files(_item(), creds=_creds(), files=files)
    assert uploaded == ["img"] and post.call_count == 1
    [attachment] = json.loads(post.calls[0].request.content)["attachments"]
    assert attachment == {
        "contentType": "image/png", "name": "chart.png",
        "contentUrl": "data:image/png;base64,iVBORw==",
    }


# ---------------------------------------------------------------------------
# typing
# ---------------------------------------------------------------------------


@respx.mock
async def test_keep_typing_posts_typing_until_the_turn_ends(platform, monkeypatch):
    monkeypatch.setattr(teams_mod, "_TYPING_EVERY_S", 0.01)
    _mock_token(respx)
    post = respx.post(ACTIVITIES).mock(return_value=httpx.Response(201, json={"id": "t"}))
    msg = parse(_activity(), app_id=APP_ID)
    turns = iter([True, True, False])

    async def active() -> bool:
        return next(turns, False)

    platform.keep_typing(msg, creds=_creds(), turn_active=active)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not platform._typing:
            break
    assert post.call_count == 3
    assert all(json.loads(c.request.content) == {"type": "typing"} for c in post.calls)
    assert not platform._typing


def test_capabilities_and_descriptor():
    p = TeamsPlatform()
    assert p.kind == "teams" and p.topology == "webhook" and p.supports_edit
    assert p.route_path() == "/teams/{app_id}" and p.route_path(APP_ID) == f"/teams/{APP_ID}"
    assert p.descriptor.vault_refs(APP_ID) == {"app_password": "app_password", "tenant_id": "tenant_id"}
    assert p.descriptor.webhook_registration == "manual"
    assert p.identifier_of(_request(None), None) == APP_ID


def test_the_teams_prompt_hint_loads_and_teams_has_no_artifact_panel():
    from surogates.harness.prompt import CHANNELS_WITHOUT_ARTIFACT_PANEL
    from surogates.harness.prompt_library import PromptLibrary

    hint = PromptLibrary().platform_hint("teams")
    assert hint and "Microsoft Teams" in hint and "MEDIA:" in hint
    assert "teams" in CHANNELS_WITHOUT_ARTIFACT_PANEL
