"""A world posts Meta-shaped, Meta-signed webhooks that the real parser accepts."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
import respx

from surogates.rehearsal.webhook import envelope, post_webhook

PNID = "9990000000000001"


def test_envelope_is_meta_shaped():
    payload = envelope(PNID, {"messages": [{"id": "wamid.1"}]})
    change = payload["entry"][0]["changes"][0]
    assert payload["object"] == "whatsapp_business_account"
    assert change["field"] == "messages"
    assert change["value"]["metadata"]["phone_number_id"] == PNID
    assert change["value"]["messages"] == [{"id": "wamid.1"}]


@pytest.mark.asyncio
async def test_post_signs_with_the_app_secret():
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=True) as router:
            route = router.post(f"http://channels:8003/whatsapp/{PNID}").mock(
                return_value=httpx.Response(200),
            )
            status = await post_webhook(client, channels_url="http://channels:8003/",
                                        phone_number_id=PNID, app_secret="s3cret",
                                        value={"statuses": []})
    request = route.calls[0].request
    expected = hmac.new(b"s3cret", request.content, hashlib.sha256).hexdigest()
    assert status == 200
    assert request.headers["x-hub-signature-256"] == f"sha256={expected}"
    assert json.loads(request.content)["entry"][0]["changes"][0]["value"]["statuses"] == []
