"""``POST /v1/auth/me/password`` lets a local (database) account change
its password; Firebase-backed accounts are refused (they reset through
their identity provider)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import bcrypt
import pytest
from fastapi import HTTPException

from surogates.api.routes.auth import ChangePasswordRequest, change_password


def _db_user(password: str = "oldpassword", provider: str = "database"):
    return SimpleNamespace(
        id="u-1",
        org_id="o-1",
        auth_provider=provider,
        password_hash=bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt(rounds=4)
        ).decode("utf-8"),
    )


def _request_for(user) -> tuple[MagicMock, MagicMock]:
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    @asynccontextmanager
    async def factory():
        yield session

    request = MagicMock()
    request.app.state.session_factory = factory
    return request, session


@pytest.mark.asyncio
async def test_change_password_success_updates_hash_and_commits():
    user = _db_user("oldpassword")
    old_hash = user.password_hash
    request, session = _request_for(user)

    result = await change_password(
        ChangePasswordRequest(
            current_password="oldpassword", new_password="newpassword1"
        ),
        request,
        tenant=MagicMock(),
    )

    assert result is None
    assert user.password_hash != old_hash
    assert bcrypt.checkpw(
        b"newpassword1", user.password_hash.encode("utf-8")
    )
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_change_password_rejects_wrong_current():
    user = _db_user("oldpassword")
    request, session = _request_for(user)

    with pytest.raises(HTTPException) as exc:
        await change_password(
            ChangePasswordRequest(
                current_password="wrong", new_password="newpassword1"
            ),
            request,
            tenant=MagicMock(),
        )

    assert exc.value.status_code == 403
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_password_rejects_short_new_password():
    request, session = _request_for(_db_user("oldpassword"))

    with pytest.raises(HTTPException) as exc:
        await change_password(
            ChangePasswordRequest(
                current_password="oldpassword", new_password="short"
            ),
            request,
            tenant=MagicMock(),
        )

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_change_password_refuses_firebase_accounts():
    user = _db_user("oldpassword", provider="firebase:proj-1")
    request, session = _request_for(user)

    with pytest.raises(HTTPException) as exc:
        await change_password(
            ChangePasswordRequest(
                current_password="oldpassword", new_password="newpassword1"
            ),
            request,
            tenant=MagicMock(),
        )

    assert exc.value.status_code == 409
    session.commit.assert_not_awaited()
