"""A voice rehearsal: the Studio user phones the agent from their browser.

There is no world to stand in for: the browser joins a LiveKit room as a SIP
caller does, carrying the attributes LiveKit SIP sets (the number called, the
caller's number, the call id), and the room dispatches the same voice agent a
real call gets.  The voice worker resolves the run's ``+999`` number from the
runtime DB and keeps the call inside the run.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

#: A call this long would have hit any line's limit anyway.
TOKEN_TTL = timedelta(hours=1)


def room_prefix(run) -> str:
    return f"rehearsal-{run.id}-"


def public_url(settings) -> str:
    return settings.rehearsal.livekit_public_url or settings.voice.livekit_url


def call_token(settings, run, person) -> dict[str, Any]:
    """What the browser needs to place one call to the run's number, as *person*."""
    from livekit import api

    vs = settings.voice
    room = room_prefix(run) + secrets.token_hex(4)
    token = (
        api.AccessToken(vs.livekit_api_key, vs.livekit_api_secret)
        .with_identity(person.address)
        .with_name(person.name)
        .with_kind("sip")
        .with_attributes({
            "sip.trunkPhoneNumber": run.phone_number_id,
            "sip.phoneNumber": person.address,
            "sip.callID": room,
        })
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True))
        .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=vs.agent_name)]))
        .with_ttl(TOKEN_TTL)
        .to_jwt()
    )
    return {"url": public_url(settings), "token": token, "room": room}


async def hang_up_all(settings, run) -> None:
    """End the run's calls still in progress, as the phone network would on a line taken away."""
    from livekit import api

    vs = settings.voice
    lk = api.LiveKitAPI(vs.livekit_url.replace("ws", "http", 1), vs.livekit_api_key, vs.livekit_api_secret)
    try:
        rooms = (await lk.room.list_rooms(api.ListRoomsRequest())).rooms
        for room in rooms:
            if room.name.startswith(room_prefix(run)):
                await lk.room.delete_room(api.DeleteRoomRequest(room=room.name))
    finally:
        await lk.aclose()
