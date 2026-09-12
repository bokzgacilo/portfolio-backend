"""Convert an uploaded audio file to mp3/wav/ogg/flac/aac via ffmpeg."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from service.common import int_env, safe_download_stem

AUDIO_MAX_UPLOAD_BYTES = int_env("AUDIO_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
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

PROTECTED_PATHS = {"/api/convert-audio"}

router = APIRouter()


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


@router.post("/api/convert-audio")
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

    safe_name = safe_download_stem(original_name, "audio")
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
