"""Convert an uploaded image (including HEIC/AVIF) to PNG, JPEG, or WebP."""

from __future__ import annotations

import io
import time

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from service.background_remover import ALLOWED_CONTENT_TYPES, MAX_UPLOAD_BYTES

register_heif_opener()

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

PROTECTED_PATHS = {"/api/convert-image"}

router = APIRouter()


@router.post("/api/convert-image")
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
