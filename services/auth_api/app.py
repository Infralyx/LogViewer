import hmac
import logging
import os
import ssl
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field
from ldap3 import Connection, NONE, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import escape_rdn

from audit import ensure_audit_table, record_audit_event
from session_auth import (
    COOKIE_NAME,
    create_session_token,
    get_authenticated_user,
    read_session_token,
    session_ttl_seconds,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("logviewer.auth")


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


def _client_details(request: Request) -> dict:
    return {
        "client_ip": request.headers.get("x-real-ip") or (request.client.host if request.client else None),
        "user_agent": request.headers.get("user-agent", "")[:1000],
    }


def _ldap_server() -> tuple[Server, bool, bool]:
    uri = os.getenv("LDAP_SERVER_URI", "").strip()
    if not uri:
        raise RuntimeError("LDAP_SERVER_URI is not configured")

    parsed = urlparse(uri)
    if parsed.scheme not in {"ldap", "ldaps"} or not parsed.hostname:
        raise RuntimeError("LDAP_SERVER_URI must use ldap:// or ldaps:// and include a hostname")

    use_ssl = parsed.scheme == "ldaps"
    default_port = 636 if use_ssl else 389
    validate_tls = os.getenv("LDAP_TLS_VALIDATE", "true").strip().lower() not in {"0", "false", "no"}
    ca_file = os.getenv("LDAP_CA_CERT_FILE", "").strip() or None
    tls = Tls(
        validate=ssl.CERT_REQUIRED if validate_tls else ssl.CERT_NONE,
        ca_certs_file=ca_file,
    )
    server = Server(
        parsed.hostname,
        port=parsed.port or default_port,
        use_ssl=use_ssl,
        tls=tls,
        get_info=NONE,
        connect_timeout=5,
    )
    starttls = os.getenv("LDAP_STARTTLS", "false").strip().lower() in {"1", "true", "yes"}
    if use_ssl and starttls:
        raise RuntimeError("Set LDAP_STARTTLS=false when LDAP_SERVER_URI uses ldaps://")
    allow_insecure = os.getenv("LDAP_ALLOW_INSECURE", "false").strip().lower() in {"1", "true", "yes"}
    if not use_ssl and not starttls and not allow_insecure:
        raise RuntimeError("Use ldaps:// or enable LDAP_STARTTLS; plain LDAP is disabled by default")
    return server, use_ssl, starttls


def _open_connection(server: Server, *, user: str | None = None, password: str | None = None, starttls: bool = False) -> Connection:
    connection = Connection(
        server,
        user=user,
        password=password,
        auto_bind=False,
        raise_exceptions=False,
        receive_timeout=5,
    )
    if not connection.open():
        raise RuntimeError("Could not open LDAP connection")
    if starttls and not connection.start_tls():
        connection.unbind()
        raise RuntimeError("LDAP StartTLS negotiation failed")
    return connection


def _find_user_dn(server: Server, starttls: bool, username: str) -> str:
    bind_dn = os.getenv("LDAP_BIND_DN", "").strip()
    bind_password = os.getenv("LDAP_BIND_PASSWORD", "")
    user_dn_template = os.getenv("LDAP_USER_DN_TEMPLATE", "").strip()

    if bind_dn or bind_password:
        if not bind_dn or not bind_password:
            raise RuntimeError("Set both LDAP_BIND_DN and LDAP_BIND_PASSWORD, or leave both empty")
        base_dn = os.getenv("LDAP_BASE_DN", "").strip()
        filter_template = os.getenv("LDAP_USER_FILTER", "").strip() or "(sAMAccountName={username})"
        if not base_dn:
            raise RuntimeError("LDAP_BASE_DN is required when using LDAP search bind")
        if "{username}" not in filter_template:
            raise RuntimeError("LDAP_USER_FILTER must contain {username}")

        connection = _open_connection(server, user=bind_dn, password=bind_password, starttls=starttls)
        try:
            if not connection.bind():
                raise RuntimeError("LDAP service-account bind failed; verify LDAP_BIND_DN and LDAP_BIND_PASSWORD")
            search_filter = filter_template.replace("{username}", escape_filter_chars(username))
            if not connection.search(base_dn, search_filter, attributes=["distinguishedName"]):
                return ""
            if len(connection.entries) != 1:
                if len(connection.entries) > 1:
                    raise RuntimeError("LDAP_USER_FILTER matched multiple users; make the filter more specific")
                return ""
            return connection.entries[0].entry_dn
        finally:
            connection.unbind()

    if user_dn_template:
        if "{username}" not in user_dn_template:
            raise RuntimeError("LDAP_USER_DN_TEMPLATE must contain {username}")
        return user_dn_template.replace("{username}", escape_rdn(username))

    raise RuntimeError(
        "Configure LDAP_BIND_DN and LDAP_BIND_PASSWORD with LDAP_BASE_DN/LDAP_USER_FILTER, "
        "or configure LDAP_USER_DN_TEMPLATE"
    )


def _authenticate_ldap(username: str, password: str) -> bool:
    try:
        server, _use_ssl, starttls = _ldap_server()
        user_dn = _find_user_dn(server, starttls, username)
        if not user_dn:
            return False
        connection = _open_connection(server, user=user_dn, password=password, starttls=starttls)
        try:
            if connection.bind():
                return True
            result = connection.result or {}
            # LDAP result code 49 means invalid credentials.
            if result.get("result") == 49:
                return False
            raise RuntimeError(f"LDAP user bind failed with result code {result.get('result', 'unknown')}")
        finally:
            connection.unbind()
    except HTTPException:
        raise
    except (LDAPException, OSError, RuntimeError, ValueError) as exc:
        logger.warning("LDAP authentication could not be completed: %s", exc.__class__.__name__)
        raise HTTPException(status_code=503, detail="LDAP authentication is unavailable; contact your administrator") from exc


def _local_superadmin_configured() -> tuple[str, str]:
    return os.getenv("SUPERADMIN_USERNAME", "").strip(), os.getenv("SUPERADMIN_PASSWORD", "")


def _set_session(response: Response, username: str, auth_source: str) -> None:
    token = create_session_token(username, auth_source)
    secure_cookie = os.getenv("SESSION_COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes"}
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=session_ttl_seconds(),
        httponly=True,
        secure=secure_cookie,
        samesite="strict",
        path="/",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Fail startup when deployment secrets are missing or still placeholders.
    superadmin_username, superadmin_password = _local_superadmin_configured()
    if not superadmin_username or len(superadmin_password) < 12 or superadmin_password.lower().startswith(("change-me", "replace-with")):
        raise RuntimeError("Set SUPERADMIN_USERNAME and a unique SUPERADMIN_PASSWORD of at least 12 characters in .env")
    database_password = os.getenv("PGPASSWORD", "")
    if not database_password or database_password.lower().startswith(("change-me", "replace-with")):
        raise RuntimeError("Set POSTGRES_PASSWORD to a unique database password in .env")
    ensure_audit_table()
    create_session_token("startup-check", "local")
    yield


app = FastAPI(title="LogViewer Authentication API", version="2.0.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "service": "auth-api", "ldap_configured": bool(os.getenv("LDAP_SERVER_URI", "").strip())}


@app.post("/login")
def login(credentials: LoginRequest, request: Request, response: Response):
    username = credentials.username.strip()
    if not username or not credentials.password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    details = _client_details(request)
    configured_superadmin, configured_password = _local_superadmin_configured()

    # If the submitted username is the configured local superadmin, never fall
    # back to LDAP after a local-password failure.
    if configured_superadmin and hmac.compare_digest(username, configured_superadmin):
        if not configured_password:
            raise HTTPException(status_code=503, detail="Local superadmin credentials are not configured")
        if not hmac.compare_digest(credentials.password, configured_password):
            record_audit_event("auth.login", username=username, auth_source="local", success=False, **details)
            raise HTTPException(status_code=401, detail="Invalid username or password")
        auth_source = "local"
    else:
        if not _authenticate_ldap(username, credentials.password):
            record_audit_event("auth.login", username=username, auth_source="ldap", success=False, **details)
            raise HTTPException(status_code=401, detail="Invalid username or password")
        auth_source = "ldap"

    record_audit_event("auth.login", username=username, auth_source=auth_source, success=True, **details)
    _set_session(response, username, auth_source)
    return {
        "authenticated": True,
        "user": {
            "username": username,
            "auth_source": auth_source,
            "is_local_superadmin": auth_source == "local",
        },
    }


@app.get("/me")
def me(user: dict = Depends(get_authenticated_user)):
    return {
        "authenticated": True,
        "user": {
            "username": user["username"],
            "auth_source": user["auth_source"],
            "is_local_superadmin": user["is_local_superadmin"],
        },
    }


@app.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME, "")
    user = read_session_token(token) if token else None
    if user:
        try:
            record_audit_event(
                "auth.logout",
                username=user.get("username"),
                auth_source=user.get("auth_source"),
                success=True,
                **_client_details(request),
            )
        except Exception:
            logger.exception("Could not record logout audit event")
    response.delete_cookie(key=COOKIE_NAME, path="/", httponly=True, samesite="strict")
    return {"authenticated": False}
