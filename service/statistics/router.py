"""Per-tool usage counters: visits/completions for tools, opens for blog posts.

Each resource_key is registered explicitly in STATISTICS_RESOURCES so a
client can only report against a known tool/post, not grow the table with
arbitrary keys.
"""

from __future__ import annotations

import json
import sqlite3
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from service.common import statistics_connection

STATISTICS_RESOURCES = (
    ("tool/audio/audio-clipper", "tool"),
    ("tool/audio/audio-converter", "tool"),
    ("tool/image/background-remover", "tool"),
    ("tool/image/image-compressor", "tool"),
    ("tool/image/image-resizer", "tool"),
    ("tool/image/image-extension-converter", "tool"),
    ("tool/converter/pdf-to-image", "tool"),
    ("tool/data/json-formatter", "tool"),
    ("tool/data/excel-unlocker", "tool"),
    ("tool/video/youtube-downloader", "tool"),
    ("blog/integrating-salesforce-crm-leads-with-a-next-js-page-router-app-7b29bac20ea9", "blog"),
)

PROTECTED_PATHS = {"/api/statistics"}

router = APIRouter()

_error: str | None = None


def init_db() -> None:
    global _error
    try:
        with statistics_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS statistic_resources (
                  resource_key TEXT PRIMARY KEY,
                  kind TEXT NOT NULL CHECK (kind IN ('tool', 'blog'))
                );
                CREATE TABLE IF NOT EXISTS resource_events (
                  event_id TEXT PRIMARY KEY,
                  resource_key TEXT NOT NULL REFERENCES statistic_resources(resource_key),
                  visitor_id TEXT NOT NULL,
                  event_type TEXT NOT NULL CHECK (event_type IN ('visit', 'complete', 'open')),
                  created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS resource_unique_visit
                  ON resource_events(resource_key, visitor_id) WHERE event_type = 'visit';
                CREATE INDEX IF NOT EXISTS resource_events_resource_idx
                  ON resource_events(resource_key, visitor_id);
                CREATE INDEX IF NOT EXISTS resource_events_rate_idx
                  ON resource_events(visitor_id, created_at);
                """
            )
            connection.executemany(
                "INSERT OR IGNORE INTO statistic_resources(resource_key, kind) VALUES (?, ?)",
                STATISTICS_RESOURCES,
            )
        _error = None
    except Exception as cause:
        _error = str(cause)


def is_ready() -> bool:
    return _error is None


def _read_statistics() -> list[dict[str, int | str]]:
    with statistics_connection() as connection:
        rows = connection.execute(
            """
            SELECT resource_key,
              COUNT(DISTINCT visitor_id) AS visitors,
              SUM(CASE WHEN event_type = 'complete' THEN 1 ELSE 0 END) AS completed,
              SUM(CASE WHEN event_type = 'open' THEN 1 ELSE 0 END) AS opens
            FROM statistic_resources
            LEFT JOIN resource_events USING (resource_key)
            GROUP BY resource_key
            ORDER BY resource_key
            """
        ).fetchall()
    return [
        {
            "resource_key": row["resource_key"],
            "visitors": int(row["visitors"] or 0),
            "completed": int(row["completed"] or 0),
            "opens": int(row["opens"] or 0),
        }
        for row in rows
    ]


def _record_statistics_event(resource: str, visitor: str, event_id: str, event: str) -> None:
    UUID(visitor)
    UUID(event_id)
    resource_kind = next((kind for key, kind in STATISTICS_RESOURCES if key == resource), None)
    if resource_kind is None or (resource_kind == "tool" and event not in {"visit", "complete"}) or (resource_kind == "blog" and event != "open"):
        raise ValueError("Invalid statistics event")
    with statistics_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM resource_events WHERE event_id = ?", (event_id,)).fetchone():
            connection.commit()
            return
        if event == "visit" and connection.execute(
            "SELECT 1 FROM resource_events WHERE resource_key = ? AND visitor_id = ? AND event_type = 'visit'",
            (resource, visitor),
        ).fetchone():
            connection.commit()
            return
        recent = connection.execute(
            "SELECT COUNT(*) FROM resource_events WHERE visitor_id = ? AND created_at >= datetime('now', '-1 day')",
            (visitor,),
        ).fetchone()[0]
        if recent >= 1000:
            connection.rollback()
            raise ValueError("Event limit reached")
        connection.execute(
            "INSERT INTO resource_events(event_id, resource_key, visitor_id, event_type) VALUES (?, ?, ?, ?)",
            (event_id, resource, visitor, event),
        )
        connection.commit()


@router.get("/api/statistics")
def read_statistics():
    if not is_ready():
        return JSONResponse(status_code=503, content={"error": "Statistics storage is unavailable."})
    try:
        return {"stats": _read_statistics()}
    except sqlite3.Error:
        return JSONResponse(status_code=503, content={"error": "Statistics storage is unavailable."})


@router.post("/api/statistics", status_code=204)
async def record_statistics(request: Request):
    if not is_ready():
        return JSONResponse(status_code=503, content={"error": "Statistics storage is unavailable."})
    try:
        body = json.loads((await request.body()).decode("utf-8"))
        if not isinstance(body, dict) or len(json.dumps(body)) > 1024:
            raise ValueError("Invalid event")
        resource, visitor, event_id, event = (body.get(name) for name in ("resource", "visitor", "eventId", "event"))
        if not all(isinstance(value, str) for value in (resource, visitor, event_id, event)):
            raise ValueError("Invalid event")
        _record_statistics_event(resource, visitor, event_id, event)
        return Response(status_code=204)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JSONResponse(status_code=400, content={"error": "Invalid statistics event."})
    except sqlite3.Error:
        return JSONResponse(status_code=503, content={"error": "Statistics storage is unavailable."})
