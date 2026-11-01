import os
from datetime import datetime, timezone
import json
from typing import AsyncIterator

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from session_auth import get_authenticated_user
from audit import ensure_audit_table, record_audit_event

app = FastAPI(title="Log Download API", version="1.0.0")


def _base_url() -> str:
    value = os.getenv("VICTORIALOGS_URL", "").strip().rstrip("/")
    if not value:
        raise HTTPException(status_code=503, detail="VICTORIALOGS_URL is not configured")
    return value


def _quote_logs_ql(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ") + '"'


def _build_query(server_ip: str | None, instance: str | None, filename: str | None, search: str | None) -> str:
    parts = ["*"]
    for field, value in (("server_ip", server_ip), ("instance", instance), ("filename", filename)):
        if value and value.strip():
            parts.append(f"{field}:={_quote_logs_ql(value.strip())}")
    if search and search.strip():
        parts.append(f"_msg:i({_quote_logs_ql(search.strip())})")
    return " ".join(parts)


def _validate_range(start: str, end: str) -> None:
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="start and end must be valid ISO-8601 timestamps")
    if start_dt >= end_dt:
        raise HTTPException(status_code=400, detail="start must be earlier than end")


@app.on_event("startup")
def initialize_audit_database():
    ensure_audit_table()


@app.get("/health")
def health():
    return {"status": "ok", "service": "download-api", "victorialogs_configured": bool(os.getenv("VICTORIALOGS_URL", "").strip())}


@app.get("/")
def root():
    return {"service": "download-api", "message": "Time-range export API is ready", "endpoint": "/export"}


@app.get("/export")
async def export_logs(
    start: str,
    end: str,
    request: Request,
    server_ip: str | None = None,
    instance: str | None = None,
    filename: str | None = None,
    search: str | None = None,
    user: dict = Depends(get_authenticated_user),
):
    _validate_range(start, end)
    # Record the export request without storing the raw search text.
    try:
        record_audit_event(
            "logs.export.requested",
            username=user["username"],
            auth_source=user["auth_source"],
            success=True,
            client_ip=(request.headers.get("x-real-ip") if request else None) or (request.client.host if request and request.client else None),
            user_agent=request.headers.get("user-agent", "")[:1000] if request else None,
            details={
                "server_ip": server_ip,
                "instance": instance,
                "filename": filename,
                "start": start,
                "end": end,
                "search_filter_used": bool(search and search.strip()),
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Audit database is unavailable; export cannot proceed") from exc

    # VictoriaLogs returns unsorted results by default. Explicitly sort by event
    # time so the downloaded file is oldest-to-newest. Sorting can use server
    # resources, but avoids loading the entire export into this API's memory.
    query = _build_query(server_ip, instance, filename, search) + " | sort by (_time)"
    timeout = float(os.getenv("VICTORIALOGS_TIMEOUT", "30"))
    client = httpx.AsyncClient(timeout=timeout)
    try:
        outbound_request = client.build_request(
            "POST",
            _base_url() + "/select/logsql/query",
            data={"query": query, "start": start, "end": end},
            headers={"Accept": "application/stream+json, application/json, text/plain"},
        )
        response = await client.send(outbound_request, stream=True)
        response.raise_for_status()
    except httpx.TimeoutException:
        await client.aclose()
        raise HTTPException(status_code=504, detail="VictoriaLogs query timed out while exporting")
    except httpx.HTTPStatusError as exc:
        await exc.response.aread()
        detail = exc.response.text[:500] or f"HTTP {exc.response.status_code}"
        await exc.response.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"VictoriaLogs returned an error: {detail}")
    except httpx.RequestError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Cannot connect to VictoriaLogs: {exc.__class__.__name__}")

    async def output_lines() -> AsyncIterator[str]:
        try:
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    # Ignore non-JSON protocol noise rather than exporting it as a log line.
                    continue
                # Export only the original message; do not add the VictoriaLogs
                # timestamp or stream metadata. Rows without _msg are not log messages.
                if "_msg" not in row or row["_msg"] is None:
                    continue
                message = str(row["_msg"])
                # Empty lines in the source log can become this VictoriaLogs diagnostic.
                if message.startswith(
                    "missing _msg field; see "
                    "https://docs.victoriametrics.com/victorialogs/keyconcepts/"
                ):
                    continue
                yield message + "\n"
        finally:
            await response.aclose()
            await client.aclose()

    headers = {"Content-Disposition": 'attachment; filename="log-export.txt"'}
    return StreamingResponse(output_lines(), media_type="text/plain; charset=utf-8", headers=headers)
