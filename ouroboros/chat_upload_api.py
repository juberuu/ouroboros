"""Chat file attachment API — upload and delete endpoints for data/uploads/."""
import mimetypes
import os
import pathlib
import uuid

from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse


_CHAT_UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
_CHUNK = 64 * 1024  # 64 KB


def _data_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get(
        "OUROBOROS_DATA_DIR",
        pathlib.Path.home() / "Ouroboros" / "data",
    ))


async def api_chat_upload(request: Request) -> JSONResponse:
    """Upload a file attachment; saved to data/uploads/ with a unique name."""
    # Content-Length pre-check (honest clients; helps before multipart parsing).
    # Wrap conversion — header is client-controlled and may be non-numeric.
    try:
        cl = int(request.headers.get("content-length", 0) or 0)
    except (ValueError, TypeError):
        cl = 0
    if cl > _CHAT_UPLOAD_MAX_BYTES + 4096:
        return JSONResponse({"ok": False, "error": "File exceeds 50 MB limit"}, status_code=413)

    # Inject a byte-counting receive wrapper so python-multipart itself
    # enforces the size limit before spooling the full body to disk.
    _original_receive = request._receive
    _body_bytes = 0

    async def _size_limited_receive():
        nonlocal _body_bytes
        msg = await _original_receive()
        _body_bytes += len(msg.get("body", b""))
        if _body_bytes > _CHAT_UPLOAD_MAX_BYTES + 8192:
            raise Exception("oversized")
        return msg

    request._receive = _size_limited_receive
    try:
        form = await request.form()
    except Exception:
        request._receive = _original_receive
        return JSONResponse({"ok": False, "error": "File exceeds 50 MB limit"}, status_code=413)
    finally:
        request._receive = _original_receive

    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        return JSONResponse({"ok": False, "error": "No valid file field"}, status_code=400)

    raw_name = getattr(upload, "filename", "") or "upload"
    safe_base = os.path.basename(raw_name).replace(" ", "_")[:200] or "upload"

    # Always use a unique stored name — avoids conflicts for repeated uploads
    # and eliminates 409-orphan blocking. Full UUID hex (32 chars) used to
    # guarantee uniqueness even under concurrent uploads of same filename.
    unique_name = f"{uuid.uuid4().hex}_{safe_base}"

    upload_dir = _data_dir() / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / unique_name

    # Unique temp file per request for concurrent-safe atomic publish
    tmp_dest = upload_dir / f".{uuid.uuid4().hex}.uploading"
    bytes_written = 0
    too_large = False
    try:
        with tmp_dest.open("wb") as fh:
            while True:
                chunk = await upload.read(_CHUNK)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > _CHAT_UPLOAD_MAX_BYTES:
                    too_large = True
                    break
                fh.write(chunk)
        if too_large:
            tmp_dest.unlink(missing_ok=True)
            return JSONResponse({"ok": False, "error": "File exceeds 50 MB limit"}, status_code=413)
        tmp_dest.replace(dest)  # atomic; unique name guarantees no collision
    finally:
        await upload.close()
        if tmp_dest.exists():
            tmp_dest.unlink(missing_ok=True)

    mime = mimetypes.guess_type(safe_base)[0] or "application/octet-stream"
    return JSONResponse({
        "ok": True,
        "filename": unique_name,          # stored name (used for delete)
        "display_name": safe_base,        # original display name for UI
        "path": str(dest),
        "size": bytes_written,
        "mime": mime,
    })


async def api_chat_upload_delete(request: Request) -> JSONResponse:
    """Delete a previously uploaded chat attachment from data/uploads/."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "JSON body must be an object"}, status_code=400)

    filename = str(body.get("filename", "")).strip()
    if not filename:
        return JSONResponse({"ok": False, "error": "Missing filename"}, status_code=400)

    safe_name = os.path.basename(filename)
    if not safe_name or safe_name != filename or safe_name in {".", ".."}:
        return JSONResponse({"ok": False, "error": "Invalid filename"}, status_code=400)

    target = _data_dir() / "uploads" / safe_name
    if not target.exists():
        return JSONResponse({"ok": False, "error": "File not found"}, status_code=404)
    if not target.is_file():
        return JSONResponse({"ok": False, "error": "Invalid filename"}, status_code=400)

    target.unlink()
    return JSONResponse({"ok": True, "filename": safe_name})
