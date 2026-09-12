"""Strip worksheet/workbook protection from an XLSX/XLSM, decrypting first
if it's password-protected."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from service.common import int_env

EXCEL_MAX_UPLOAD_BYTES = int_env("EXCEL_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
EXCEL_EXTENSIONS = {".xlsx", ".xlsm"}
EXCEL_MEDIA_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
}

PROTECTED_PATHS = {"/api/unlock-excel"}

router = APIRouter()


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


@router.post("/api/unlock-excel")
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
