"""API gateway.

Each tool lives in its own service/<name>/ package (router, config, and
helpers together) so a change to one tool never touches another's file. This
module only wires them into one FastAPI app: CORS, the origin-allowlist
middleware, startup/shutdown, and the two catch-all endpoints (/ and
/health) that summarize every service at once.

Run locally:  uvicorn main:app --reload
Run on Render: uvicorn main:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from service import (
    audio_converter,
    background_remover,
    excel_unlocker,
    image_converter,
    statistics,
    youtube_downloader,
)
from service.common import ALLOWED_ORIGIN_REGEX, ALLOWED_ORIGINS, STRICT_ORIGIN_CHECK

PROTECTED_PATHS = (
    background_remover.PROTECTED_PATHS
    | image_converter.PROTECTED_PATHS
    | excel_unlocker.PROTECTED_PATHS
    | audio_converter.PROTECTED_PATHS
    | youtube_downloader.PROTECTED_PATHS
    | statistics.PROTECTED_PATHS
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    statistics.init_db()
    youtube_downloader.init_db()
    youtube_downloader.startup()
    background_remover.startup()
    yield
    background_remover.shutdown()


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
    if STRICT_ORIGIN_CHECK and request.url.path in PROTECTED_PATHS:
        origin = request.headers.get("origin")
        if origin not in ALLOWED_ORIGINS:
            return JSONResponse(status_code=403, content={"error": "Origin not allowed."})

    return await call_next(request)


app.include_router(background_remover.router)
app.include_router(image_converter.router)
app.include_router(excel_unlocker.router)
app.include_router(audio_converter.router)
app.include_router(youtube_downloader.router)
app.include_router(statistics.router)


@app.get("/")
def root():
    return {
        "service": "background-remover",
        "model": background_remover.MODEL_NAME,
        "endpoints": {
            "health": "GET /health",
            "statistics": "GET/POST /api/statistics",
            "remove": "POST /api/remove-background",
            "convert": "POST /api/convert-image",
            "convertAudio": "POST /api/convert-audio",
            "unlockExcel": "POST /api/unlock-excel",
            "youtubeInfo": "POST /api/youtube/info",
            "youtubeDownload": "POST /api/youtube/download",
            "youtubeFile": "GET /api/youtube/file/{jobId}",
        },
        "limits": {
            "maxUploadBytes": background_remover.MAX_UPLOAD_BYTES,
            "maxDimension": background_remover.MAX_DIMENSION,
            "acceptedTypes": sorted(background_remover.ALLOWED_CONTENT_TYPES),
            "convertAcceptedTypes": sorted(image_converter.CONVERT_ALLOWED_CONTENT_TYPES),
            "convertOutputTypes": sorted(image_converter.CONVERT_OUTPUT_TYPES),
            "excelMaxUploadBytes": excel_unlocker.EXCEL_MAX_UPLOAD_BYTES,
            "excelExtensions": sorted(excel_unlocker.EXCEL_EXTENSIONS),
            "audioMaxUploadBytes": audio_converter.AUDIO_MAX_UPLOAD_BYTES,
            "audioInputExtensions": sorted(audio_converter.AUDIO_INPUT_EXTENSIONS),
            "audioOutputFormats": sorted(audio_converter.AUDIO_OUTPUT_FORMATS),
            "youtubeMaxDurationSeconds": youtube_downloader.YOUTUBE_MAX_DURATION_SECONDS,
            "youtubeFormats": sorted(youtube_downloader.YOUTUBE_FORMATS),
            "youtubeMaxHeight": youtube_downloader.YOUTUBE_MAX_HEIGHT,
            "youtubeLinkTtlSeconds": youtube_downloader.YOUTUBE_LINK_TTL_SECONDS,
        },
    }


@app.get("/health")
def health():
    """Cheap and side-effect free. The frontend pings this to wake a sleeping
    free-tier instance before the visitor has picked a file."""
    bg = background_remover.health()
    return {
        "status": "ok",
        "model": background_remover.MODEL_NAME,
        "ready": bg["ready"],
        "backgroundRemovalError": bg["error"],
        "statisticsReady": statistics.is_ready(),
        "youtubeReady": youtube_downloader.is_ready(),
    }


# Every failure answers with the same {"error": ...} shape, so the frontend has
# one thing to read instead of FastAPI's `detail` in two different layouts.
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_, __: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={"error": "Send the upload as multipart/form-data under the field name \"file\"."},
    )
