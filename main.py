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
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from rembg import new_session, remove

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

# Comma-separated list. The defaults cover local Next dev and the production
# site; Vercel preview URLs are matched by the regex below instead.
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,https://www.bokzgacilo.com,https://bokzgacilo.com",
    ).split(",")
    if origin.strip()
]

# Every Vercel preview deployment gets its own hostname, so an allowlist of
# literal origins can never keep up with them.
ALLOWED_ORIGIN_REGEX = os.getenv(
    "ALLOWED_ORIGIN_REGEX", r"https://.*\.vercel\.app"
)

# --------------------------------------------------------------------------- #
# App                                                                         #
# --------------------------------------------------------------------------- #

# One session, built at startup and reused. Building it per request would
# re-read the model off disk every time; Render also gives the container its
# first traffic only after startup finishes, so paying the cost here means the
# first real request is not the one that waits.
_session = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _session
    _session = new_session(MODEL_NAME)
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
    ],
)


@app.get("/")
def root():
    return {
        "service": "background-remover",
        "model": MODEL_NAME,
        "endpoints": {"health": "GET /health", "remove": "POST /api/remove-background"},
        "limits": {
            "maxUploadBytes": MAX_UPLOAD_BYTES,
            "maxDimension": MAX_DIMENSION,
            "acceptedTypes": sorted(ALLOWED_CONTENT_TYPES),
        },
    }


@app.get("/health")
def health():
    """Cheap and side-effect free. The frontend pings this to wake a sleeping
    free-tier instance before the visitor has picked a file."""
    return {"status": "ok", "model": MODEL_NAME, "ready": _session is not None}


@app.post("/api/remove-background")
async def remove_background(file: UploadFile = File(...)):
    if _session is None:  # pragma: no cover - startup ran, so this is defensive
        raise HTTPException(status_code=503, detail="Model is still loading.")

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
        content={"error": "Send the image as multipart/form-data under the field name \"file\"."},
    )
