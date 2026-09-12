"""Strip the background from an uploaded photo and return a transparent PNG.

The heavy lifting is rembg (a U^2-Net ONNX model); everything here is the
guard rail around it -- type checks, size caps, and a downscale pass so one
large upload cannot exhaust a small instance's memory.
"""

from __future__ import annotations

import io
import os
import time

# Must be set before rembg (which pulls in numba) is imported anywhere in the
# process, so this has to happen at the top of this module rather than main.py.
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba-cache")

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageOps, UnidentifiedImageError
from rembg import new_session, remove

from service.common import int_env

# u2netp is the small U^2-Net (~4.7 MB). It fits Render's free 512 MB instance
# with room to spare. On a paid instance set REMBG_MODEL=isnet-general-use (or
# u2net) for noticeably cleaner edges on hair and fur.
MODEL_NAME = os.getenv("REMBG_MODEL", "u2netp")

MAX_UPLOAD_BYTES = int_env("MAX_UPLOAD_BYTES", 10 * 1024 * 1024)

# Longest edge we will run the model on. Bigger inputs are downscaled first:
# the model itself only ever sees 320x320, so past this point extra pixels buy
# nothing but RAM. The response says what it actually produced.
MAX_DIMENSION = int_env("MAX_DIMENSION", 2000)

ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
}

PROTECTED_PATHS = {"/api/remove-background"}

router = APIRouter()

# One session, built at startup and reused. Building it per request would
# re-read the model off disk every time; Render also gives the container its
# first traffic only after startup finishes, so paying the cost here means the
# first real request is not the one that waits.
_session = None
_session_error: str | None = None


def _load_session():
    return new_session(MODEL_NAME, providers=["CPUExecutionProvider"])


def startup() -> None:
    global _session, _session_error
    try:
        _session = _load_session()
        _session_error = None
    except Exception as cause:
        _session = None
        _session_error = str(cause)


def shutdown() -> None:
    global _session
    _session = None


def health() -> dict:
    return {"ready": _session is not None, "error": _session_error}


@router.post("/api/remove-background")
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
