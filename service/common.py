"""Cross-cutting config and helpers shared by more than one service.

Anything used by only one tool belongs in that tool's own service/<name>/
router.py instead -- keep this file small so it stays easy to scan.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path

from fastapi import HTTPException, Request

logger = logging.getLogger("api")


def configure_logging() -> None:
    """Plain stdout logging -- systemd/journalctl captures it as-is, and
    Render/uvicorn's own stdout capture works the same way. Call once at
    startup; safe to call more than once (idempotent)."""
    if logger.handlers:
        return
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def client_ip(request: Request) -> str:
    """Best-effort real client IP. The API sits behind a Cloudflare tunnel, so
    request.client.host is the tunnel daemon's loopback address, not the
    visitor -- prefer the headers Cloudflare actually sets."""
    forwarded = request.headers.get("cf-connecting-ip")
    if forwarded:
        return forwarded
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "-"


def int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# Comma-separated list. The defaults cover local Next dev and the production
# site; Vercel preview URLs are matched by the regex below instead.
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        "https://www.bokzgacilo.com,https://bokzgacilo.com,http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if origin.strip()
]

# Blank by default so production only accepts the literal domains above. Set
# this explicitly in local/dev environments if preview origins are needed.
ALLOWED_ORIGIN_REGEX = os.getenv("ALLOWED_ORIGIN_REGEX") or None

STRICT_ORIGIN_CHECK = os.getenv("STRICT_ORIGIN_CHECK", "1").lower() not in {
    "0",
    "false",
    "no",
}

STATISTICS_DB_PATH = Path(
    os.getenv("STATISTICS_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "statistics.sqlite3"))
).expanduser()


def statistics_connection() -> sqlite3.Connection:
    """The one sqlite file every service's tables live in. Each service owns
    and creates only its own tables here -- see statistics/router.py and
    youtube_downloader/router.py's init_db()."""
    STATISTICS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(STATISTICS_DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def safe_download_stem(filename: str | None, default: str) -> str:
    base_name = Path(filename or default).stem[:80]
    return re.sub(r"[^A-Za-z0-9._ -]+", "-", base_name).strip(" .-") or default


async def read_json_body(request: Request) -> dict:
    try:
        body = json.loads((await request.body()).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as cause:
        raise HTTPException(status_code=400, detail="Send a JSON request body.") from cause
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Send a JSON request body.")
    return body
