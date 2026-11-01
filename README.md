# LogViewer V2 — PostgreSQL, LDAP and local superadmin

This V2 update adds authentication and a PostgreSQL audit database while preserving the existing React UI, VictoriaLogs query behavior, browser-timezone display, pagination, auto-refresh, and streamed chronological exports.

## V2 behavior

- Users sign in with corporate LDAP/Active Directory credentials.
- A local superadmin account is configured using `SUPERADMIN_USERNAME` and `SUPERADMIN_PASSWORD` in `.env`. It does not depend on LDAP availability.
- All authenticated users can view, search, and download logs. V2 does not implement roles or per-user permissions; those are deferred to V3.
- Session cookies are signed, HttpOnly, SameSite=Strict, and expire after `SESSION_TTL_SECONDS` (default 30 minutes).
- FastAPI protects both log-query and export endpoints. The frontend login screen is not the security boundary; the APIs verify the session independently.
- PostgreSQL stores audit events for successful/failed logins, logout, and log-export requests. It does not store application logs or LDAP passwords.
- VictoriaLogs remains the only log-storage/search backend.
- The export API skips the `missing _msg field; see https://docs.victoriametrics.com/victorialogs/keyconcepts/` diagnostic messages produced from empty source-log lines. Normal messages, JSP source lines, Java exceptions, and stack traces are preserved.
- PostgreSQL is not published on a host port. Its data persists in the `logviewer_postgres_data` Docker volume.

## 1. Update the existing `.env`

**Do not replace your current `.env`**, because it already contains the working `VICTORIALOGS_URL`. This ZIP intentionally does not include `.env` or any credentials.

Copy the new settings below into your existing `C:\LogViewer\.env`, then replace every placeholder:

```dotenv
POSTGRES_DB=logviewer
POSTGRES_USER=logviewer_app
POSTGRES_PASSWORD=CHANGE-ME-use-a-long-random-database-password

SUPERADMIN_USERNAME=superadmin
SUPERADMIN_PASSWORD=CHANGE-ME-use-a-unique-long-password

SESSION_SECRET_KEY=CHANGE-ME-generate-a-random-secret-of-at-least-32-characters
SESSION_TTL_SECONDS=1800
SESSION_COOKIE_SECURE=false

LDAP_SERVER_URI=ldaps://your-domain-controller.example.internal:636
LDAP_BASE_DN=DC=example,DC=internal
LDAP_USER_FILTER=(sAMAccountName={username})
LDAP_BIND_DN=CN=logviewer-reader,OU=Service Accounts,DC=example,DC=internal
LDAP_BIND_PASSWORD=CHANGE-ME-read-only-LDAP-service-account-password
LDAP_USER_DN_TEMPLATE=
LDAP_STARTTLS=false
LDAP_ALLOW_INSECURE=false
LDAP_TLS_VALIDATE=true
LDAP_CA_CERT_FILE=
```

Keep your existing `VICTORIALOGS_URL`, `VICTORIALOGS_TIMEOUT`, and `LOGVIEWER_PORT` settings. Set `POSTGRES_PASSWORD`, `SUPERADMIN_PASSWORD`, and `LDAP_BIND_PASSWORD` to unique secrets. Do not use the example values in a real deployment.

Generate a session-signing secret from PowerShell using Python:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Copy the generated value into `SESSION_SECRET_KEY`. Do not reuse the database or superadmin password as the session secret.

For local HTTP testing, keep `SESSION_COOKIE_SECURE=false`. When LogViewer is served over HTTPS, set `SESSION_COOKIE_SECURE=true`.

## 2. Configure LDAP

The example above is for Active Directory. Your LDAP administrator must provide the actual values for your directory.

- `LDAP_SERVER_URI`: LDAP/LDAPS endpoint. Prefer `ldaps://...:636`, or use `ldap://...:389` with StartTLS enabled.
- `LDAP_BASE_DN`: base distinguished name used to search for users.
- `LDAP_USER_FILTER`: Active Directory commonly uses `(sAMAccountName={username})`. OpenLDAP commonly uses `(uid={username})`.
- `LDAP_BIND_DN` and `LDAP_BIND_PASSWORD`: preferably a dedicated, read-only service account that can search for user DNs. Set both or leave both empty.
- `LDAP_USER_DN_TEMPLATE`: alternative to service-account search when user DNs follow a predictable pattern. Example: `uid={username},ou=people,dc=example,dc=internal`. Use this instead of service-account bind settings, not alongside them.
- `LDAP_STARTTLS=true`: use only with `ldap://` when your directory supports StartTLS. Plain LDAP without StartTLS is disabled by default. `LDAP_ALLOW_INSECURE=true` explicitly permits it only if your environment requires it; do not use this in production.
- `LDAP_TLS_VALIDATE=true`: keep certificate validation enabled. For a private/internal CA, copy the CA certificate to `C:\LogViewer\certs\ldap-ca.crt` and set `LDAP_CA_CERT_FILE=/app/certs/ldap-ca.crt`. The `certs` folder is mounted read-only into the authentication container.

If LDAP is not configured yet, the local superadmin can still log in. LDAP users will not be able to log in until the directory settings are correct. Invalid credentials return 401; an unreachable or incorrectly configured LDAP service returns 503.

## 3. Build and start V2

From PowerShell:

```powershell
cd C:\LogViewer
docker compose config
docker compose up --build -d
```

The first startup creates the PostgreSQL volume and `audit_events` table. PostgreSQL data remains in the named volume when containers are rebuilt.

Open `http://localhost:8080` (or the port configured by `LOGVIEWER_PORT`) and sign in with the configured local superadmin credentials. Once LDAP is configured, test with a directory user too.

## 4. Verify services

```powershell
docker compose ps
docker compose logs --tail=100 postgres auth-api logs-api download-api frontend
```

Authentication health endpoint through NGINX:

```powershell
Invoke-RestMethod http://localhost:8080/api/auth/health
```

The log APIs require a valid session. Direct requests to `/api/logs/query` or `/api/download/export` without logging in should return HTTP 401.

## 5. PostgreSQL audit table

The application creates `audit_events` automatically. It records event type, username, authentication source, success/failure, client IP, user agent, selected non-sensitive export filters, and timestamp. It deliberately does not store passwords or the raw search text.

To inspect the table from the PostgreSQL container:

```powershell
docker compose exec postgres psql -U logviewer_app -d logviewer -c "SELECT id, event_type, username, auth_source, success, client_ip, created_at FROM audit_events ORDER BY id DESC LIMIT 20;"
```

Replace the database/user values in that command if you changed `POSTGRES_DB` or `POSTGRES_USER`.

## 6. Session and secret notes

- Sessions expire after 30 minutes by default. Change `SESSION_TTL_SECONDS` if required (minimum 300 seconds, maximum 86400 seconds).
- If you change `SESSION_SECRET_KEY`, all existing sessions become invalid and users must log in again.
- The local superadmin credentials are read from `.env`; they are not stored in PostgreSQL. Use a unique strong password and restrict access to the `.env` file.
- Do not publish PostgreSQL's port to the host unless there is a specific operational requirement.
- Do not set `LDAP_TLS_VALIDATE=false` in production.
- There is no role-based access control in V2: every authenticated LDAP user and the local superadmin can use the same LogViewer features. RBAC is deferred to V3.

## Files changed

- Added `services/auth_api` for login, logout, session identity, and LDAP validation.
- Added `services/common/session_auth.py` for shared signed-session validation.
- Added `services/common/audit.py` for PostgreSQL audit storage.
- Updated Docker Compose to add PostgreSQL and the authentication API.
- Updated NGINX and React for login/logout.
- Protected log-query and log-export API endpoints.
- Added export filtering for VictoriaLogs empty-line diagnostic records.
- `.env` is intentionally excluded; merge settings into the existing file manually.
