"""Signed values the runtime exchanges with the document server."""

from __future__ import annotations

import time
from typing import Any, Literal
from uuid import UUID

from jose import JWTError, jwt

_ALGORITHM = "HS256"
FILE_TOKEN_SECONDS = 15 * 60

Purpose = Literal["file", "callback"]


class InvalidOfficeToken(Exception):
    """A token that is forged, expired, unsigned or for another purpose."""


def sign(secret: str, claims: dict[str, Any]) -> str:
    return jwt.encode(claims, secret, algorithm=_ALGORITHM)


def url_token(secret: str, row_id: UUID, purpose: Purpose) -> str:
    """The token in a file or callback URL: names the row and what the URL is for.

    File tokens expire; the document server fetches the file when the editing
    session starts.  Callback tokens do not: a save can come days later, and
    the row being closed is what retires them.
    """
    now = int(time.time())
    claims: dict[str, Any] = {"sub": str(row_id), "pur": purpose, "iat": now}
    if purpose == "file":
        claims["exp"] = now + FILE_TOKEN_SECONDS
    return sign(secret, claims)


def read_url_token(secret: str, token: str, purpose: Purpose) -> UUID:
    try:
        claims = jwt.decode(token, secret, algorithms=[_ALGORITHM])
    except JWTError as exc:
        raise InvalidOfficeToken(str(exc)) from None
    if claims.get("pur") != purpose:
        raise InvalidOfficeToken("the token is for another purpose")
    try:
        return UUID(str(claims["sub"]))
    except (KeyError, ValueError):
        raise InvalidOfficeToken("the token names no row") from None


def read_document_server_body(secret: str, body: dict[str, Any], authorization: str | None) -> dict[str, Any]:
    """The callback the document server signed.

    The token is in the body's ``token`` (JWT_IN_BODY) or in the Authorization
    header, where the body is wrapped in ``payload``.
    """
    token = body.get("token")
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
    if not token:
        raise InvalidOfficeToken("the callback is not signed")
    try:
        claims = jwt.decode(token, secret, algorithms=[_ALGORITHM])
    except JWTError as exc:
        raise InvalidOfficeToken(str(exc)) from None
    payload = claims.get("payload")
    return payload if isinstance(payload, dict) else claims
