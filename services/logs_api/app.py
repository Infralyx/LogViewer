import os
from datetime import datetime, timezone
from typing import Any

import httpx
from session_auth import get_authenticated_user
from fastapi import Depends, FastAPI, HTTPException, Query

app = FastAPI(title="Log Query API", version="1.0.0")


def _base_url() -> str:
    value = os.getenv("VICTORIALOGS_URL", "").strip().rstrip("/")
    if not value:
        raise HTTPException(status_code=503, detail="VICTORIALOGS_URL is not configured")
    return value


def _quote_logs_ql(value: str) -> str:
    """Quote a value for a LogsQL string filter."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ") + '"'


def _build_query(server_ip: str | None, instance: str | None, filename: str | None, search: str | None, has_range: bool) -> str:
    parts = []
    if not has_range:
        parts.append("_time:15m")
    else:
        parts.append("*")
    for field, value in (("server_ip", server_ip), ("instance", instance), ("filename", filename)):
        if value and value.strip():
            parts.append(f"{field}:={_quote_logs_ql(value.strip())}")
    if search and search.strip():
        # A quoted LogsQL term searches for the phrase in the message field.
        parts.append(f"_msg:i({_quote_logs_ql(search.strip())})")
    return " ".join(parts)


def _validate_range(start: str | None, end: str | None) -> bool:
    if bool(start) != bool(end):
        raise HTTPException(status_code=400, detail="Provide both start and end, or neither")
    if start and end:
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
        return True
    return False


async def _query_victorialogs(params: dict[str, Any]) -> list[dict[str, Any]]:
    url = _base_url() + "/select/logsql/query"
    timeout = float(os.getenv("VICTORIALOGS_TIMEOUT", "30"))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, data=params, headers={"Accept": "application/stream+json, application/json, text/plain"})
            response.raise_for_status()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="VictoriaLogs query timed out")
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500] or f"HTTP {exc.response.status_code}"
        raise HTTPException(status_code=502, detail=f"VictoriaLogs returned an error: {detail}")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot connect to VictoriaLogs: {exc.__class__.__name__}")

    entries: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        if not line.strip():
            continue
        try:
            item = __import__("json").loads(line)
        except ValueError:
            continue
        # Only return the fields required by the UI. Never expose _stream or _stream_id.
        entries.append({
            "time": item.get("_time", ""),
            "message": item.get("_msg", ""),
            "server_ip": item.get("server_ip", ""),
            "instance": item.get("instance", ""),
            "filename": item.get("filename", ""),
            "job": item.get("job", ""),
        })
    return entries


@app.get("/health")
def health():
    return {"status": "ok", "service": "logs-api", "victorialogs_configured": bool(os.getenv("VICTORIALOGS_URL", "").strip())}


@app.get("/")
def root():
    return {"service": "logs-api", "message": "VictoriaLogs query API is ready", "endpoint": "/query"}


@app.get("/query")
async def query_logs(
    start: str | None = None,
    end: str | None = None,
    server_ip: str | None = None,
    instance: str | None = None,
    filename: str | None = None,
    search: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=1000000),
    _user: dict = Depends(get_authenticated_user),
):
    has_range = _validate_range(start, end)
    params: dict[str, Any] = {
        "query": _build_query(server_ip, instance, filename, search, has_range),
        "limit": limit,
        "offset": offset,
    }
    if has_range:
        params["start"] = start
        params["end"] = end
    logs = await _query_victorialogs(params)
    return {"logs": logs, "count": len(logs), "limit": limit, "offset": offset}
