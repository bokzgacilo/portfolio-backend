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

os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba-cache")

from fastapi import FastAPI, File, HTTPException, UploadFile
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
    global _session, _session_error
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
    ],
)


@app.middleware("http")
async def enforce_api_origin(request, call_next):
    protected_paths = {"/api/remove-background", "/api/convert-image"}
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
            "remove": "POST /api/remove-background",
            "convert": "POST /api/convert-image",
        },
        "limits": {
            "maxUploadBytes": MAX_UPLOAD_BYTES,
            "maxDimension": MAX_DIMENSION,
            "acceptedTypes": sorted(ALLOWED_CONTENT_TYPES),
            "convertAcceptedTypes": sorted(CONVERT_ALLOWED_CONTENT_TYPES),
            "convertOutputTypes": sorted(CONVERT_OUTPUT_TYPES),
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
    }


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
        content={"error": "Send the image as multipart/form-data under the field name \"file\"."},
    )
