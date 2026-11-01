import hashlib
import os
from typing import Any

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "logviewer_session"
SESSION_SALT = "logviewer-auth-session-v1"


def _serializer() -> URLSafeTimedSerializer:
    secret = os.getenv("SESSION_SECRET_KEY", "").strip()
    if len(secret) < 32 or secret.lower().startswith(("change-me", "replace-with", "your-secret")):
        raise HTTPException(
            status_code=503,
            detail="SESSION_SECRET_KEY must be configured with a random secret of at least 32 characters",
        )
    return URLSafeTimedSerializer(
        secret_key=secret,
        salt=SESSION_SALT,
        signer_kwargs={"digest_method": hashlib.sha256},
    )


def session_ttl_seconds() -> int:
    try:
        value = int(os.getenv("SESSION_TTL_SECONDS", "1800"))
    except ValueError:
        value = 1800
    return max(300, min(value, 86400))


def create_session_token(username: str, auth_source: str) -> str:
    return _serializer().dumps({"username": username, "auth_source": auth_source})


def read_session_token(token: str) -> dict[str, Any] | None:
    try:
        data = _serializer().loads(token, max_age=session_ttl_seconds())
    except (BadSignature, SignatureExpired, HTTPException):
        return None
    if not isinstance(data, dict) or not data.get("username") or data.get("auth_source") not in {"ldap", "local"}:
        return None
    return data


def get_authenticated_user(request: Request) -> dict[str, Any]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")

    user = read_session_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid; please log in again")

    user["is_local_superadmin"] = user["auth_source"] == "local"
    return user
