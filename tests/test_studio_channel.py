"""The studio channel: who may declare it, and the historical relabel.

``studio`` and ``api`` authenticate identically — the only thing telling
them apart is that the control plane's live-chat forwarder declares the
former.  That makes two things worth pinning: an end-user must not be able
to label their own session as an operator's, and the backfill that fixes
historical rows must be safe to run repeatedly against live data.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from surogates.api.routes.sessions import CreateSessionRequest
from surogates.channels.constants import (
    API_CHANNEL,
    SERVICE_ACCOUNT_CHANNELS,
    STUDIO_CHANNEL,
)
from surogates.db.studio_channel import RELABEL_STUDIO_SESSIONS_SQL


def test_channel_defaults_to_none_so_existing_clients_land_on_api():
    # Every third-party client predates the field. Absent must keep
    # meaning API_CHANNEL, which the route spells `body.channel or
    # API_CHANNEL` — a default of "api" here would work too, but None
    # keeps "did the caller ask?" answerable.
    assert CreateSessionRequest().channel is None


@pytest.mark.parametrize("channel", sorted(SERVICE_ACCOUNT_CHANNELS))
def test_service_account_channels_are_declarable(channel):
    assert CreateSessionRequest(channel=channel).channel == channel


@pytest.mark.parametrize(
    "channel",
    ["web", "website", "slack", "telegram", "whatsapp", "task", "worker",
     "delegation", "scheduled", "ambient", "", "STUDIO", "studio "],
)
def test_non_service_account_channels_are_rejected(channel):
    # The web route hardcodes channel="web" and never reads this field,
    # so this validator is the second line of defence rather than the
    # first — but a caller that could declare "web" on the API route
    # would produce a session that looks like an end-user's.
    with pytest.raises(ValidationError):
        CreateSessionRequest(channel=channel)


def test_relabel_only_touches_rows_still_carrying_the_old_label():
    # Idempotence is what makes this safe to run on every migrate: the
    # WHERE clause must exclude rows the previous run already converted.
    assert f"s.channel = '{API_CHANNEL}'" in RELABEL_STUDIO_SESSIONS_SQL
    assert f"SET channel = '{STUDIO_CHANNEL}'" in RELABEL_STUDIO_SESSIONS_SQL


def test_relabel_requires_an_ops_chat_service_account():
    # Without the join, an end-user's web chat or a third-party API
    # client would be swept into the operator bucket.
    assert "service_accounts" in RELABEL_STUDIO_SESSIONS_SQL
    assert "sa.id = s.service_account_id" in RELABEL_STUDIO_SESSIONS_SQL
    assert "ops-chat-%" in RELABEL_STUDIO_SESSIONS_SQL


def test_membership_alone_does_not_entitle_a_caller_to_studio():
    # The validator answers "is this value spellable", not "may this
    # caller use it". The route asks the second question separately via
    # _is_ops_chat_account, because any service-account token passes the
    # first one — and a third party filing its traffic under the
    # operator's own conversations would empty the label of meaning.
    import inspect

    from surogates.api.routes import sessions as route

    src = inspect.getsource(route.create_api_session)
    assert "is_ops_chat_service_account" in src
    assert "STUDIO_CHANNEL" in src


def test_ops_chat_proof_is_shared_with_owner_scope():
    # One implementation of the name proof, not two: the route and the
    # owner-scope check must never disagree about who counts as the
    # control plane.
    import inspect

    from surogates.tools import owner_scope

    assert inspect.iscoroutinefunction(owner_scope.is_ops_chat_service_account)
    assert "is_ops_chat_service_account" in inspect.getsource(
        owner_scope.is_owner_scoped
    )
