"""Look up a YouTube video, then download it as MP4 (<=720p) or MP3.

Flow: POST /info creates a job row (id, url, metadata) and returns its id.
POST /download looks that job up by id (so a client can never swap in a
different URL later), runs yt-dlp once, converts locally for mp3, and hands
back a link rather than the file itself. GET /file/{job_id} serves it until
the job's expires_at passes.

Only one yt-dlp process runs at a time (_yt_dlp_lock): concurrent runs would
share the same cookies file and pile onto one small instance's CPU/bandwidth,
so a second request just waits its turn.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from service.common import int_env, logger, read_json_body, safe_download_stem, statistics_connection

# Longest video the downloader will process. A free-tier instance has no
# business transcoding a two-hour video; this also bounds worst-case memory
# and request time.
YOUTUBE_MAX_DURATION_SECONDS = int_env("YOUTUBE_MAX_DURATION_SECONDS", 1800)
YOUTUBE_URL_RE = re.compile(
    r"^https?://(www\.|m\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)[\w-]{6,}",
    re.IGNORECASE,
)
YOUTUBE_FORMATS = {"mp3", "mp4"}
YOUTUBE_MAX_HEIGHT = int_env("YOUTUBE_MAX_HEIGHT", 720)

# Scratch space for the yt-dlp/ffmpeg subprocess output before the finished
# file is moved into the public download directory below.
YOUTUBE_TMP_DIR = Path(os.getenv("YOUTUBE_TMP_DIR", "/tmp/youtube-scratch"))

# Files here are served by GET /api/youtube/file/{job_id} until their row's
# expires_at passes; _purge_expired_jobs deletes both the row and the file at
# that point.
YOUTUBE_DOWNLOADS_DIR = Path(
    os.getenv("YOUTUBE_DOWNLOADS_DIR", str(Path(__file__).resolve().parent.parent.parent / "data" / "youtube-downloads"))
).expanduser()

YOUTUBE_LINK_TTL_SECONDS = int_env("YOUTUBE_LINK_TTL_SECONDS", 3600)

YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE") or None

PROTECTED_PATHS = {"/api/youtube/info", "/api/youtube/download"}
# /api/youtube/file/{job_id} is deliberately excluded from PROTECTED_PATHS: it
# is reached by a plain download navigation (<a href>), which sends no Origin
# header, and its uuid4 job id is already unguessable.

router = APIRouter()

# Only one yt-dlp process runs at a time -- see module docstring.
_yt_dlp_lock = asyncio.Lock()

_error: str | None = None


def init_db() -> None:
    global _error
    try:
        with statistics_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS youtube_jobs (
                  id TEXT PRIMARY KEY,
                  url TEXT NOT NULL,
                  title TEXT,
                  thumbnail TEXT,
                  channel TEXT,
                  duration_seconds INTEGER,
                  status TEXT NOT NULL CHECK (status IN ('pending', 'ready')),
                  format TEXT,
                  file_path TEXT,
                  created_at TEXT NOT NULL DEFAULT (datetime('now')),
                  expires_at TEXT
                );
                CREATE INDEX IF NOT EXISTS youtube_jobs_expires_idx ON youtube_jobs(expires_at);
                """
            )
        _error = None
    except Exception as cause:
        _error = str(cause)


def is_ready() -> bool:
    return _error is None


def startup() -> None:
    YOUTUBE_TMP_DIR.mkdir(parents=True, exist_ok=True)
    YOUTUBE_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    _purge_expired_jobs()


def _validate_url(url: str | None) -> str:
    url = (url or "").strip()
    if not url or not YOUTUBE_URL_RE.match(url):
        raise HTTPException(status_code=400, detail="Enter a valid YouTube video URL.")
    return url


def _run_yt_dlp(args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    """Run the yt-dlp CLI (not the Python library) with the cookies file that
    has tested clean of YouTube's bot check on this VPS, plus a player-client
    fallback chain for when a client alone still trips detection."""
    yt_dlp_path = shutil.which("yt-dlp")
    if yt_dlp_path is None:
        raise RuntimeError("yt-dlp is not installed on the server.")
    command = [yt_dlp_path]
    if YOUTUBE_COOKIES_FILE:
        command += ["--cookies", YOUTUBE_COOKIES_FILE]
    command += ["--extractor-args", "youtube:player_client=tv,ios,android,web"]
    # A URL copied from a playlist/radio/mix (?list=...&start_radio=1) would
    # otherwise make yt-dlp try to resolve the whole list instead of the one
    # video, which can run well past the request timeout.
    command += ["--no-playlist"]
    command += args
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


async def _run_yt_dlp_exclusive(fn, /, *args):
    """Run a blocking yt-dlp-calling function off the event loop, one caller
    at a time. A second concurrent request awaits the lock instead of
    launching its own subprocess."""
    async with _yt_dlp_lock:
        return await asyncio.to_thread(fn, *args)


def _yt_dlp_error(result: subprocess.CompletedProcess, fallback: str) -> str:
    stderr = (result.stderr or "").strip()
    return stderr.splitlines()[-1][:200] if stderr else fallback


def _fetch_info(url: str) -> dict:
    try:
        result = _run_yt_dlp(["--skip-download", "--no-warnings", "-J", url], timeout=30)
    except subprocess.TimeoutExpired as cause:
        raise ValueError("Timed out reading that video.") from cause
    if result.returncode != 0:
        raise ValueError(_yt_dlp_error(result, "Could not read that video."))
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as cause:
        raise ValueError("Could not read that video.") from cause


def _download_video(url: str, job_id: str, target: str, ffmpeg_path: str) -> Path:
    """Download once via the yt-dlp CLI and, for mp3, extract audio from that
    same local file -- never a second fetch from YouTube."""
    YOUTUBE_TMP_DIR.mkdir(parents=True, exist_ok=True)
    output_template = str(YOUTUBE_TMP_DIR / f"{job_id}.%(ext)s")
    args = ["--no-warnings", "--ffmpeg-location", ffmpeg_path, "-o", output_template]
    if target == "mp3":
        args += ["-f", "bestaudio/best", "--extract-audio", "--audio-format", "mp3", "--audio-quality", "192K"]
    else:
        args += [
            "-f",
            f"bestvideo[height<={YOUTUBE_MAX_HEIGHT}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={YOUTUBE_MAX_HEIGHT}][ext=mp4]/best[height<={YOUTUBE_MAX_HEIGHT}]",
            "--merge-output-format",
            "mp4",
        ]
    args.append(url)

    try:
        result = _run_yt_dlp(args, timeout=600)
    except subprocess.TimeoutExpired as cause:
        raise ValueError("The download timed out.") from cause
    if result.returncode != 0:
        raise ValueError(_yt_dlp_error(result, "Could not download that video."))

    produced = sorted(YOUTUBE_TMP_DIR.glob(f"{job_id}.*"))
    if not produced:
        raise ValueError("The download produced no output file.")
    return produced[-1]


def _get_job(job_id: str) -> sqlite3.Row | None:
    with statistics_connection() as connection:
        return connection.execute("SELECT * FROM youtube_jobs WHERE id = ?", (job_id,)).fetchone()


def _purge_expired_jobs() -> None:
    """Delete rows (and their files) past expires_at, plus lookups that were
    never turned into a download. Run at the top of every endpoint instead of
    a separate cron -- this instance is too small to need one."""
    with statistics_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, file_path FROM youtube_jobs
            WHERE (expires_at IS NOT NULL AND expires_at < datetime('now'))
               OR (status = 'pending' AND created_at < datetime('now', '-6 hours'))
            """
        ).fetchall()
        for row in rows:
            if row["file_path"]:
                Path(row["file_path"]).unlink(missing_ok=True)
        connection.execute(
            """
            DELETE FROM youtube_jobs
            WHERE (expires_at IS NOT NULL AND expires_at < datetime('now'))
               OR (status = 'pending' AND created_at < datetime('now', '-6 hours'))
            """
        )


@router.post("/api/youtube/info")
async def youtube_info(request: Request):
    """Look up a YouTube video's title/thumbnail/duration and open a job for
    it. The job id is what /api/youtube/download and /api/youtube/file key
    off of, so a client can never swap in a different URL after this point."""
    _purge_expired_jobs()
    body = await read_json_body(request)
    url = _validate_url(body.get("url"))

    try:
        info = await _run_yt_dlp_exclusive(_fetch_info, url)
    except (ValueError, RuntimeError) as cause:
        # The client only gets a generic message -- it may quote back
        # anything yt-dlp printed, including account/session details -- so
        # the real reason (bot check, expired cookies, private video, ...)
        # only survives here in the log.
        logger.warning("youtube info failed for %s: %s", url, cause)
        raise HTTPException(
            status_code=422,
            detail="Could not read that video. It may be private, age-restricted, or unavailable.",
        ) from cause

    duration = int(info.get("duration") or 0)
    title = info.get("title") or "video"
    job_id = str(uuid4())
    with statistics_connection() as connection:
        connection.execute(
            """
            INSERT INTO youtube_jobs (id, url, title, thumbnail, channel, duration_seconds, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (job_id, url, title, info.get("thumbnail"), info.get("uploader"), duration),
        )

    return {
        "jobId": job_id,
        "title": title,
        "thumbnail": info.get("thumbnail"),
        "channel": info.get("uploader"),
        "durationSeconds": duration,
        "tooLong": duration > YOUTUBE_MAX_DURATION_SECONDS,
        "maxDurationSeconds": YOUTUBE_MAX_DURATION_SECONDS,
    }


@router.post("/api/youtube/download")
async def youtube_download(request: Request):
    """Convert a looked-up job to MP4 (<=720p) or MP3 and hand back a link to
    it, rather than the file itself -- large videos would otherwise have to
    round-trip through the frontend's proxy route and its size/time limits."""
    _purge_expired_jobs()
    body = await read_json_body(request)
    job_id = str(body.get("jobId") or "").strip()
    target = str(body.get("format") or "").strip().lower()
    if target not in YOUTUBE_FORMATS:
        raise HTTPException(status_code=415, detail="Choose mp3 or mp4 as the format.")

    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="That video lookup has expired. Look it up again.")

    if (
        job["status"] == "ready"
        and job["format"] == target
        and job["file_path"]
        and Path(job["file_path"]).exists()
    ):
        return {
            "downloadUrl": f"/api/youtube/file/{job_id}",
            "sizeBytes": Path(job["file_path"]).stat().st_size,
            "expiresInSeconds": YOUTUBE_LINK_TTL_SECONDS,
        }

    # Duration is re-checked here even though /api/youtube/info already
    # screened it -- that check is only a UI hint.
    duration = int(job["duration_seconds"] or 0)
    if duration > YOUTUBE_MAX_DURATION_SECONDS:
        limit_minutes = YOUTUBE_MAX_DURATION_SECONDS // 60
        raise HTTPException(status_code=413, detail=f"That video is longer than the {limit_minutes}-minute limit.")

    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        raise HTTPException(status_code=503, detail="Video conversion is not installed on the server.")

    started = time.perf_counter()
    try:
        tmp_file = await _run_yt_dlp_exclusive(_download_video, job["url"], job_id, target, ffmpeg_path)
    except (ValueError, RuntimeError) as cause:
        logger.warning("youtube download failed for %s (%s): %s", job["url"], target, cause)
        raise HTTPException(
            status_code=422,
            detail="Could not download that video. It may be private, age-restricted, or unavailable.",
        ) from cause
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if tmp_file.stat().st_size == 0:
        tmp_file.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="The download produced an empty file.")

    YOUTUBE_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    final_path = YOUTUBE_DOWNLOADS_DIR / f"{job_id}{tmp_file.suffix}"
    shutil.move(str(tmp_file), str(final_path))

    with statistics_connection() as connection:
        connection.execute(
            """
            UPDATE youtube_jobs
            SET status = 'ready', format = ?, file_path = ?, expires_at = datetime('now', ?)
            WHERE id = ?
            """,
            (target, str(final_path), f"+{YOUTUBE_LINK_TTL_SECONDS} seconds", job_id),
        )

    return {
        "downloadUrl": f"/api/youtube/file/{job_id}",
        "sizeBytes": final_path.stat().st_size,
        "expiresInSeconds": YOUTUBE_LINK_TTL_SECONDS,
        "elapsedMs": elapsed_ms,
    }


@router.get("/api/youtube/file/{job_id}")
def youtube_file(job_id: str):
    """Serve a converted download until its job's expires_at passes. The job
    id is an unguessable uuid4, which is the only access control here -- there
    is no origin check, since a plain <a href> download navigation never
    sends an Origin header for the browser to enforce against."""
    _purge_expired_jobs()
    job = _get_job(job_id)
    if job is None or job["status"] != "ready" or not job["file_path"] or not Path(job["file_path"]).exists():
        raise HTTPException(status_code=404, detail="That download link has expired or does not exist.")

    file_path = Path(job["file_path"])
    extension = file_path.suffix.lstrip(".")
    media_type = "audio/mpeg" if job["format"] == "mp3" else "video/mp4"
    safe_name = safe_download_stem(job["title"], "video")
    return Response(
        content=file_path.read_bytes(),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}.{extension}"',
            "Cache-Control": "no-store",
        },
    )
