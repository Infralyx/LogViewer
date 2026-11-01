import os
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


def _database_kwargs() -> dict[str, Any]:
    password = os.getenv("PGPASSWORD", "")
    if not password:
        raise RuntimeError("POSTGRES_PASSWORD/PGPASSWORD is not configured")
    return {
        "host": os.getenv("PGHOST", "postgres"),
        "port": int(os.getenv("PGPORT", "5432")),
        "dbname": os.getenv("PGDATABASE", "logviewer"),
        "user": os.getenv("PGUSER", "logviewer_app"),
        "password": password,
        "connect_timeout": 5,
    }


def ensure_audit_table() -> None:
    with psycopg.connect(**_database_kwargs()) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id BIGSERIAL PRIMARY KEY,
                event_type VARCHAR(100) NOT NULL,
                username VARCHAR(256),
                auth_source VARCHAR(32),
                success BOOLEAN NOT NULL DEFAULT TRUE,
                client_ip INET,
                user_agent TEXT,
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_events_created_at ON audit_events (created_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_events_username ON audit_events (username, created_at DESC)"
        )


def record_audit_event(
    event_type: str,
    *,
    username: str | None = None,
    auth_source: str | None = None,
    success: bool = True,
    client_ip: str | None = None,
    user_agent: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    with psycopg.connect(**_database_kwargs()) as connection:
        connection.execute(
            """
            INSERT INTO audit_events
                (event_type, username, auth_source, success, client_ip, user_agent, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_type,
                username,
                auth_source,
                success,
                client_ip,
                user_agent,
                Jsonb(details or {}),
            ),
        )
