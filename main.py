"""
Background-removal API.

One job: take an uploaded photo, return a transparent PNG cutout. The heavy
lifting is rembg (a U^2-Net ONNX model); everything here is the guard rail
around it -- type checks, size caps, and a downscale pass so one large upload
cannot exhaust a small instance's memory.

Run locally:  uvicorn main:app --reload
Run on Render: uvicorn main:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from uuid import UUID

os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba-cache")

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener
from rembg import new_session, remove

register_heif_opener()

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# u2netp is the small U^2-Net (~4.7 MB). It fits Render's free 512 MB instance
# with room to spare. On a paid instance set REMBG_MODEL=isnet-general-use (or
# u2net) for noticeably cleaner edges on hair and fur.
MODEL_NAME = os.getenv("REMBG_MODEL", "u2netp")

MAX_UPLOAD_BYTES = _int_env("MAX_UPLOAD_BYTES", 10 * 1024 * 1024)

# Longest edge we will run the model on. Bigger inputs are downscaled first:
# the model itself only ever sees 320x320, so past this point extra pixels buy
# nothing but RAM. The response says what it actually produced.
MAX_DIMENSION = _int_env("MAX_DIMENSION", 2000)

ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
}

CONVERT_ALLOWED_CONTENT_TYPES = ALLOWED_CONTENT_TYPES | {
    "image/avif",
    "image/heic",
    "image/heif",
}

CONVERT_OUTPUT_TYPES = {
    "image/png": ("PNG", "png"),
    "image/jpeg": ("JPEG", "jpg"),
    "image/webp": ("WEBP", "webp"),
}

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
    os.getenv("STATISTICS_DB_PATH", str(Path(__file__).resolve().parent / "data" / "statistics.sqlite3"))
).expanduser()

# These keys are intentionally explicit. A client can report only resources
# registered here, so random URLs cannot grow the database indefinitely.
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
    ("blog/integrating-salesforce-crm-leads-with-a-next-js-page-router-app-7b29bac20ea9", "blog"),
)

EXCEL_MAX_UPLOAD_BYTES = _int_env("EXCEL_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
EXCEL_EXTENSIONS = {".xlsx", ".xlsm"}
EXCEL_MEDIA_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
}

AUDIO_MAX_UPLOAD_BYTES = _int_env("AUDIO_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
AUDIO_INPUT_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".flac",
    ".m4a",
    ".mp3",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".weba",
    ".wma",
}
AUDIO_OUTPUT_FORMATS = {
    "mp3": {
        "extension": "mp3",
        "media_type": "audio/mpeg",
        "ffmpeg_args": ["-codec:a", "libmp3lame", "-q:a", "2"],
    },
    "wav": {
        "extension": "wav",
        "media_type": "audio/wav",
        "ffmpeg_args": ["-codec:a", "pcm_s16le"],
    },
    "ogg": {
        "extension": "ogg",
        "media_type": "audio/ogg",
        "ffmpeg_args": ["-codec:a", "libvorbis", "-q:a", "5"],
    },
    "flac": {
        "extension": "flac",
        "media_type": "audio/flac",
        "ffmpeg_args": ["-codec:a", "flac"],
    },
    "aac": {
        "extension": "aac",
        "media_type": "audio/aac",
        "ffmpeg_args": ["-codec:a", "aac", "-b:a", "192k"],
    },
}
_statistics_error: str | None = None


def _statistics_connection() -> sqlite3.Connection:
    STATISTICS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(STATISTICS_DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def _init_statistics_db() -> None:
    with _statistics_connection() as connection:
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


def _read_statistics() -> list[dict[str, int | str]]:
    with _statistics_connection() as connection:
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
    with _statistics_connection() as connection:
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


def _remove_excel_protection(data: bytes) -> bytes:
    """Remove worksheet and workbook protection from an OOXML workbook.

    The ZIP package is rewritten without changing the other workbook parts,
    which preserves formulas, styles, charts, and VBA projects in .xlsm files.
    """
    source = io.BytesIO(data)
    if not zipfile.is_zipfile(source):
        raise ValueError("That file is not a valid Excel workbook.")

    try:
        from lxml import etree
    except ImportError as cause:
        raise RuntimeError("Excel support is not installed on the server.") from cause

    output = io.BytesIO()
    with zipfile.ZipFile(source, "r") as archive, zipfile.ZipFile(output, "w") as result:
        for info in archive.infolist():
            payload = archive.read(info.filename)
            if info.filename == "xl/workbook.xml" or (
                info.filename.startswith("xl/worksheets/") and info.filename.endswith(".xml")
            ):
                try:
                    root = etree.fromstring(
                        payload,
                        parser=etree.XMLParser(resolve_entities=False, no_network=True),
                    )
                    for protected in root.xpath(
                        "//*[local-name()='workbookProtection' or local-name()='sheetProtection']"
                    ):
                        parent = protected.getparent()
                        if parent is not None:
                            parent.remove(protected)
                    payload = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
                except etree.XMLSyntaxError as cause:
                    raise ValueError("The workbook contains invalid XML.") from cause
            result.writestr(info, payload)
    return output.getvalue()


def _excel_sheet_counts(data: bytes) -> tuple[int, int, int]:
    """Return total, protected, and unprotected worksheet counts."""
    from lxml import etree

    total = 0
    locked = 0
    with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
        worksheet_names = [name for name in archive.namelist() if name.startswith("xl/worksheets/") and name.endswith(".xml")]
        if "xl/workbook.xml" in archive.namelist():
            workbook = etree.fromstring(archive.read("xl/workbook.xml"))
            total = len(workbook.xpath("//*[local-name()='sheets']/*[local-name()='sheet']"))
        for name in worksheet_names:
            worksheet = etree.fromstring(archive.read(name))
            if worksheet.xpath("boolean(.//*[local-name()='sheetProtection'])"):
                locked += 1
    total = max(total, len(worksheet_names))
    return total, locked, max(total - locked, 0)


def _decrypt_excel(data: bytes, password: str) -> bytes:
    try:
        import msoffcrypto
    except ImportError as cause:
        raise RuntimeError("Excel encryption support is not installed on the server.") from cause
    try:
        office_file = msoffcrypto.OfficeFile(io.BytesIO(data))
        office_file.load_key(password=password)
        output = io.BytesIO()
        office_file.decrypt(output)
        return output.getvalue()
    except Exception as cause:
        raise ValueError("The password is incorrect or the workbook could not be decrypted.") from cause


def _safe_download_stem(filename: str | None, default: str) -> str:
    base_name = Path(filename or default).stem[:80]
    return re.sub(r"[^A-Za-z0-9._ -]+", "-", base_name).strip(" .-") or default


@lru_cache(maxsize=8)
def _ffmpeg_encoder_available(ffmpeg_path: str, encoder: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return any(len(parts := line.split()) > 1 and parts[1] == encoder for line in result.stdout.splitlines())


def _audio_ffmpeg_args(ffmpeg_path: str, target: str) -> list[str]:
    args = list(AUDIO_OUTPUT_FORMATS[target]["ffmpeg_args"])
    if target == "ogg" and "libvorbis" in args and not _ffmpeg_encoder_available(ffmpeg_path, "libvorbis"):
        if _ffmpeg_encoder_available(ffmpeg_path, "libopus"):
            return ["-codec:a", "libopus", "-b:a", "128k"]
        args[args.index("libvorbis")] = "vorbis"
        args.extend(["-strict", "experimental"])
    return args


def _convert_audio_file(input_path: Path, output_path: Path, target: str) -> None:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        raise RuntimeError("Audio conversion is not installed on the server.")

    command = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-vn",
        *_audio_ffmpeg_args(ffmpeg_path, target),
        str(output_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as cause:
        raise ValueError("Audio conversion timed out.") from cause
    except subprocess.CalledProcessError as cause:
        message = (cause.stderr or "").strip()
        if message:
            message = message.splitlines()[-1][:180]
        raise ValueError(message or "That file could not be converted.") from cause

# --------------------------------------------------------------------------- #
# App                                                                         #
# --------------------------------------------------------------------------- #

# One session, built at startup and reused. Building it per request would
# re-read the model off disk every time; Render also gives the container its
# first traffic only after startup finishes, so paying the cost here means the
# first real request is not the one that waits.
_session = None
_session_error = None


def _load_session():
    return new_session(MODEL_NAME, providers=["CPUExecutionProvider"])


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _session, _session_error, _statistics_error
    try:
        _init_statistics_db()
        _statistics_error = None
    except Exception as cause:
        _statistics_error = str(cause)
    try:
        _session = _load_session()
        _session_error = None
    except Exception as cause:
        _session = None
        _session_error = str(cause)
    yield
    _session = None


app = FastAPI(
    title="Background Remover API",
    description="Strips the background from an image and returns a transparent PNG.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    # Without this the browser hides them from fetch(), and the frontend reads
    # its timing and dimension receipt from these.
    expose_headers=[
        "X-Processing-Ms",
        "X-Model",
        "X-Original-Width",
        "X-Original-Height",
        "X-Output-Width",
        "X-Output-Height",
        "X-Downscaled",
        "X-Source-Format",
        "X-Output-Format",
        "X-Excel-Total-Sheets",
        "X-Excel-Locked-Sheets",
        "X-Excel-Unlocked-Sheets",
        "X-Audio-Source-Format",
        "X-Audio-Output-Format",
        "X-Audio-Output-Bytes",
    ],
)


@app.middleware("http")
async def enforce_api_origin(request, call_next):
    protected_paths = {
        "/api/remove-background",
        "/api/convert-image",
        "/api/statistics",
        "/api/unlock-excel",
        "/api/convert-audio",
    }
    if STRICT_ORIGIN_CHECK and request.url.path in protected_paths:
        origin = request.headers.get("origin")
        if origin not in ALLOWED_ORIGINS:
            return JSONResponse(status_code=403, content={"error": "Origin not allowed."})

    return await call_next(request)


@app.get("/")
def root():
    return {
        "service": "background-remover",
        "model": MODEL_NAME,
        "endpoints": {
            "health": "GET /health",
            "statistics": "GET/POST /api/statistics",
            "remove": "POST /api/remove-background",
            "convert": "POST /api/convert-image",
            "convertAudio": "POST /api/convert-audio",
            "unlockExcel": "POST /api/unlock-excel",
        },
        "limits": {
            "maxUploadBytes": MAX_UPLOAD_BYTES,
            "maxDimension": MAX_DIMENSION,
            "acceptedTypes": sorted(ALLOWED_CONTENT_TYPES),
            "convertAcceptedTypes": sorted(CONVERT_ALLOWED_CONTENT_TYPES),
            "convertOutputTypes": sorted(CONVERT_OUTPUT_TYPES),
            "excelMaxUploadBytes": EXCEL_MAX_UPLOAD_BYTES,
            "excelExtensions": sorted(EXCEL_EXTENSIONS),
            "audioMaxUploadBytes": AUDIO_MAX_UPLOAD_BYTES,
            "audioInputExtensions": sorted(AUDIO_INPUT_EXTENSIONS),
            "audioOutputFormats": sorted(AUDIO_OUTPUT_FORMATS),
        },
    }


@app.get("/health")
def health():
    """Cheap and side-effect free. The frontend pings this to wake a sleeping
    free-tier instance before the visitor has picked a file."""
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "ready": _session is not None,
        "backgroundRemovalError": _session_error,
        "statisticsReady": _statistics_error is None,
    }


@app.get("/api/statistics")
def read_statistics():
    if _statistics_error is not None:
        return JSONResponse(status_code=503, content={"error": "Statistics storage is unavailable."})
    try:
        return {"stats": _read_statistics()}
    except sqlite3.Error:
        return JSONResponse(status_code=503, content={"error": "Statistics storage is unavailable."})


@app.post("/api/statistics", status_code=204)
async def record_statistics(request: Request):
    if _statistics_error is not None:
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


@app.post("/api/unlock-excel")
async def unlock_excel(file: UploadFile = File(...), password: str = Form("")):
    """Return a copy of an XLSX/XLSM workbook with protection removed."""
    original_name = file.filename or "workbook.xlsx"
    suffix = Path(original_name).suffix.lower()
    if suffix not in EXCEL_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Upload an .xlsx or .xlsm workbook.")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded workbook was empty.")
    if len(data) > EXCEL_MAX_UPLOAD_BYTES:
        limit_mb = EXCEL_MAX_UPLOAD_BYTES / (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"That workbook is larger than the {limit_mb:.0f} MB limit.")

    try:
        if not zipfile.is_zipfile(io.BytesIO(data)):
            data = _decrypt_excel(data, password)
        total_sheets, locked_sheets, unlocked_sheets = _excel_sheet_counts(data)
        unlocked = _remove_excel_protection(data)
    except RuntimeError as cause:
        raise HTTPException(status_code=503, detail=str(cause)) from cause
    except ValueError as cause:
        raise HTTPException(status_code=400, detail=str(cause)) from cause

    base_name = Path(original_name).stem[:80]
    safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "-", base_name).strip(" .-") or "workbook"
    download_name = f"{safe_name}-unlocked{suffix}"
    return Response(
        content=unlocked,
        media_type=EXCEL_MEDIA_TYPES[suffix],
        headers={
            "Content-Disposition": f'attachment; filename="{download_name}"',
            "Cache-Control": "no-store",
            "X-Protection-Removed": "1",
            "X-Excel-Total-Sheets": str(total_sheets),
            "X-Excel-Locked-Sheets": str(locked_sheets),
            "X-Excel-Unlocked-Sheets": str(unlocked_sheets),
        },
    )


@app.post("/api/convert-audio")
async def convert_audio(file: UploadFile = File(...), target: str = Form("mp3")):
    """Convert an uploaded audio file to a visitor-selected output format."""
    target = target.strip().lower().lstrip(".")
    if target not in AUDIO_OUTPUT_FORMATS:
        raise HTTPException(
            status_code=415,
            detail="Choose mp3, wav, ogg, flac, or aac as the target format.",
        )

    original_name = file.filename or "audio"
    suffix = Path(original_name).suffix.lower()
    content_type = (file.content_type or "").lower()
    if suffix not in AUDIO_INPUT_EXTENSIONS and not content_type.startswith("audio/"):
        raise HTTPException(
            status_code=415,
            detail="Upload an audio file such as MP3, WAV, OGG, FLAC, M4A, or AAC.",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded audio file was empty.")
    if len(data) > AUDIO_MAX_UPLOAD_BYTES:
        limit_mb = AUDIO_MAX_UPLOAD_BYTES / (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"That audio file is larger than the {limit_mb:.0f} MB limit.",
        )

    format_config = AUDIO_OUTPUT_FORMATS[target]
    input_suffix = suffix if suffix in AUDIO_INPUT_EXTENSIONS else ".audio"
    with tempfile.TemporaryDirectory(prefix="audio-convert-") as directory:
        workspace = Path(directory)
        input_path = workspace / f"source{input_suffix}"
        output_path = workspace / f"converted.{format_config['extension']}"
        input_path.write_bytes(data)
        try:
            started = time.perf_counter()
            _convert_audio_file(input_path, output_path, target)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            payload = output_path.read_bytes()
        except RuntimeError as cause:
            raise HTTPException(status_code=503, detail=str(cause)) from cause
        except (OSError, ValueError) as cause:
            raise HTTPException(status_code=400, detail=str(cause)) from cause

    if not payload:
        raise HTTPException(status_code=500, detail="Audio conversion produced an empty file.")

    safe_name = _safe_download_stem(original_name, "audio")
    download_name = f"{safe_name}.{format_config['extension']}"
    return Response(
        content=payload,
        media_type=str(format_config["media_type"]),
        headers={
            "Content-Disposition": f'attachment; filename="{download_name}"',
            "Cache-Control": "no-store",
            "X-Processing-Ms": str(elapsed_ms),
            "X-Audio-Source-Format": content_type or suffix.lstrip(".") or "unknown",
            "X-Audio-Output-Format": target,
            "X-Audio-Output-Bytes": str(len(payload)),
        },
    )


@app.post("/api/convert-image")
async def convert_image(file: UploadFile = File(...), target: str = "image/png"):
    content_type = (file.content_type or "").lower()
    if content_type not in CONVERT_ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported type: {content_type or 'unknown'}. "
                "Send a JPG, PNG, WebP, AVIF, HEIC, or HEIF image."
            ),
        )

    target = target.lower()
    if target not in CONVERT_OUTPUT_TYPES:
        raise HTTPException(
            status_code=415,
            detail="Choose PNG, JPEG, or WebP as the target format.",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file was empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        limit_mb = MAX_UPLOAD_BYTES / (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"That file is larger than the {limit_mb:.0f} MB limit.",
        )

    try:
        source = Image.open(io.BytesIO(data))
        source.load()
    except (UnidentifiedImageError, OSError):
        raise HTTPException(status_code=400, detail="That file is not a readable image.")

    source = ImageOps.exif_transpose(source)
    original_width, original_height = source.size

    output_format, extension = CONVERT_OUTPUT_TYPES[target]
    if target == "image/jpeg":
        image = source.convert("RGBA")
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        background.alpha_composite(image)
        output = background.convert("RGB")
    elif target == "image/png":
        output = source.convert("RGBA")
    else:
        output = source.convert("RGBA" if source.mode in {"RGBA", "LA"} else "RGB")

    started = time.perf_counter()
    buffer = io.BytesIO()
    save_kwargs = {"optimize": True}
    if target in {"image/jpeg", "image/webp"}:
        save_kwargs["quality"] = 92
    output.save(buffer, format=output_format, **save_kwargs)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    payload = buffer.getvalue()

    base_name = (file.filename or "image").rsplit(".", 1)[0] or "image"
    safe_name = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in base_name[:80]
    ).strip("-") or "image"

    return Response(
        content=payload,
        media_type=target,
        headers={
            "X-Processing-Ms": str(elapsed_ms),
            "X-Original-Width": str(original_width),
            "X-Original-Height": str(original_height),
            "X-Output-Width": str(output.width),
            "X-Output-Height": str(output.height),
            "X-Source-Format": content_type,
            "X-Output-Format": target,
            "Content-Disposition": f'inline; filename="{safe_name}.{extension}"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/remove-background")
async def remove_background(file: UploadFile = File(...)):
    global _session, _session_error
    if _session is None:
        try:
            _session = _load_session()
            _session_error = None
        except Exception as cause:
            _session_error = str(cause)
            raise HTTPException(
                status_code=503,
                detail="Background-removal model is unavailable right now.",
            )

    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported type: {content_type or 'unknown'}. Send a JPG, PNG, or WebP.",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file was empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        limit_mb = MAX_UPLOAD_BYTES / (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"That file is larger than the {limit_mb:.0f} MB limit.",
        )

    try:
        source = Image.open(io.BytesIO(data))
        source.load()
    except (UnidentifiedImageError, OSError):
        raise HTTPException(status_code=400, detail="That file is not a readable image.")

    # EXIF orientation is applied now, so the cutout is not returned sideways.
    source = ImageOps.exif_transpose(source).convert("RGB")
    original_width, original_height = source.size

    downscaled = max(source.size) > MAX_DIMENSION
    if downscaled:
        source.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)

    started = time.perf_counter()
    try:
        cutout = remove(source, session=_session)
    except Exception as cause:  # model or memory failure -- do not leak traces
        raise HTTPException(
            status_code=500, detail=f"Background removal failed: {cause}"
        )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    buffer = io.BytesIO()
    cutout.save(buffer, format="PNG", optimize=True)
    payload = buffer.getvalue()

    return Response(
        content=payload,
        media_type="image/png",
        headers={
            "X-Processing-Ms": str(elapsed_ms),
            "X-Model": MODEL_NAME,
            "X-Original-Width": str(original_width),
            "X-Original-Height": str(original_height),
            "X-Output-Width": str(cutout.width),
            "X-Output-Height": str(cutout.height),
            "X-Downscaled": "1" if downscaled else "0",
            "Content-Disposition": 'inline; filename="cutout.png"',
            "Cache-Control": "no-store",
        },
    )


# Every failure answers with the same {"error": ...} shape, so the frontend has
# one thing to read instead of FastAPI's `detail` in two different layouts.
@app.exception_handler(HTTPException)
async def http_exception_handler(_, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_, __: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={"error": "Send the upload as multipart/form-data under the field name \"file\"."},
    )
