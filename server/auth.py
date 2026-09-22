"""Write authorization for automation clients and the first-party console.

Automation keeps using the configured Bearer token. The browser console receives
a short-lived, signed, host-only cookie that JavaScript cannot read. Console
mutations additionally require same-origin browser metadata, which keeps a
cross-site request from borrowing the ambient cookie.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request, Response

from .config import settings

CONSOLE_COOKIE = "mirageglass_console"
_SESSION_PURPOSE = b"mirageglass-console-session-v1"


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "unauthorized", "message": "Write authorization is required."},
    )


def _signing_key() -> bytes:
    """Derive a purpose-specific key without ever sending the operation token."""
    if not settings.upload_token:
        return b""
    return hashlib.sha256(
        _SESSION_PURPOSE + bytes([0]) + settings.upload_token.encode()
    ).digest()


def _origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}".lower()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _new_console_token(request: Request, now: int | None = None) -> str:
    issued_at = int(time.time()) if now is None else now
    payload = {
        "iat": issued_at,
        "nonce": secrets.token_urlsafe(18),
        "origin": _origin(request),
    }
    encoded = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = _b64encode(
        hmac.new(_signing_key(), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{encoded}.{signature}"


def _decode_console_token(token: str, request: Request) -> dict | None:
    key = _signing_key()
    if not key:
        return None
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = _b64encode(
            hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        payload = json.loads(_b64decode(encoded))
        issued_at = int(payload["iat"])
        age = int(time.time()) - issued_at
        if age < 0 or age >= settings.console_session_ttl_seconds:
            return None
        if payload.get("origin") != _origin(request):
            return None
        return payload
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def issue_console_session(request: Request, response: Response) -> None:
    """Issue or renew the HttpOnly console credential on a console document read."""
    if not settings.console_write_enabled or not settings.upload_token:
        response.delete_cookie(CONSOLE_COOKIE, path="/")
        return
    token = request.cookies.get(CONSOLE_COOKIE, "")
    payload = _decode_console_token(token, request) if token else None
    if payload is not None:
        age = int(time.time()) - int(payload["iat"])
        if age < settings.console_session_ttl_seconds // 2:
            return
    response.set_cookie(
        CONSOLE_COOKIE,
        _new_console_token(request),
        max_age=settings.console_session_ttl_seconds,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )


def _same_origin_request(request: Request) -> bool:
    expected = _origin(request)
    supplied_origin = request.headers.get("origin")
    if supplied_origin:
        return supplied_origin.rstrip("/").lower() == expected

    referer = request.headers.get("referer")
    if referer:
        parsed = urlsplit(referer)
        return f"{parsed.scheme}://{parsed.netloc}".lower() == expected

    # Modern browsers send Fetch Metadata even when they omit Origin.
    return request.headers.get("sec-fetch-site", "").lower() == "same-origin"


def has_operation_token(authorization: str) -> bool:
    if not settings.upload_token:
        return False
    return hmac.compare_digest(authorization, f"Bearer {settings.upload_token}")


def require_operation_token(authorization: str = Header(default="")) -> None:
    """Authorize automation-only writes without changing the existing header contract."""
    if not settings.upload_token:
        raise HTTPException(
            status_code=500,
            detail="MIRAGEGLASS_TOKEN is not configured.",
        )
    if not has_operation_token(authorization):
        raise _unauthorized()


def require_management_write(
    request: Request,
    authorization: str = Header(default=""),
) -> str:
    """Accept the operation token or an enabled, origin-bound console session."""
    if has_operation_token(authorization):
        return "operation_token"
    if not settings.console_write_enabled:
        raise _unauthorized()
    token = request.cookies.get(CONSOLE_COOKIE, "")
    if not token or _decode_console_token(token, request) is None:
        raise _unauthorized()
    if not _same_origin_request(request):
        raise _unauthorized()
    return "console_session"