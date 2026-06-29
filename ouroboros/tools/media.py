"""Media tools: ocr_pdf (text-layer PDF extraction) + youtube_transcript (captions).

Both are lightweight and DEPENDENCY-OPTIONAL: each returns a typed `⚠️ *_UNAVAILABLE`
string instead of raising when its optional dependency or data is absent, so a missing
dep degrades gracefully rather than burning rounds. `ocr_pdf` reuses the view_image
local-file trust boundary; `youtube_transcript` is web-gated like web_search
(`registry._WEB_TOOLS`). `extract_video_frames` (ffmpeg) is a deferred follow-up — see
docs/ARCHITECTURE.md and the v6.52.0 plan.
"""
from __future__ import annotations

import html as _html
import json as _json
import pathlib
import re
from typing import List

from ouroboros.tools.registry import ToolContext, ToolEntry

_OCR_PDF_MAX_BYTES = 25 * 1024 * 1024
_OCR_PDF_MAX_PAGES = 50
_OCR_PDF_MAX_CHARS = 200_000
_YT_HTTP_TIMEOUT_SEC = 30
_YT_MAX_CHARS = 200_000
_YT_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; OuroborosMedia/1.0)"}


def _resolve_local_file(ctx: ToolContext, path: str, *, max_bytes: int) -> tuple[pathlib.Path | None, str]:
    """Resolve a local file path through the SAME trust boundary as view_image/read_file:
    it must sit under an allowed file root and pass the protected-artifact read guard.
    Returns (path, "") on success or (None, error_message)."""
    from ouroboros.tools.vision import _allowed_file_roots
    from ouroboros.tool_access import path_is_relative_to

    text = str(path or "").strip()
    if not text:
        return None, "⚠️ TOOL_ARG_ERROR: `path` is required."
    roots = _allowed_file_roots(ctx)
    raw = pathlib.Path(text).expanduser()
    # Resolve against EVERY allowed root (not just the first) so a RELATIVE path from the
    # staged-attachment manifest — e.g. `attachments/doc.pdf`, which lives under artifact_store,
    # not the uploads root — is found wherever it actually is, matching the
    # read_file(root='artifact_store', path='attachments/...') contract the manifest advertises.
    if raw.is_absolute():
        candidates = [raw.resolve(strict=False)]
    else:
        candidates = [(r / text).resolve(strict=False) for r in roots]
    confined = [c for c in candidates if any(c == r or path_is_relative_to(c, r) for r in roots)]
    if not confined:
        return None, (
            f"⚠️ PATH_BLOCKED: {text} is outside the allowed file roots "
            "(uploads / active workspace / artifact store / task drive)."
        )
    fp = next((c for c in confined if c.is_file()), confined[0])
    try:
        from ouroboros.protected_artifacts import block_reason_for_path

        reason = block_reason_for_path(ctx, fp, "read_bytes")
        if reason:
            return None, f"⚠️ PATH_BLOCKED: {reason}"
    except Exception:  # noqa: BLE001 — guard is best-effort; the root check above is the floor
        pass
    if not fp.exists() or not fp.is_file():
        return None, f"⚠️ FILE_NOT_FOUND: {text}"
    if fp.stat().st_size > max_bytes:
        return None, f"⚠️ FILE_TOO_LARGE: {fp.stat().st_size} bytes (max {max_bytes})."
    return fp, ""


def _ocr_pdf(ctx: ToolContext, path: str = "", max_pages: int = 0) -> str:
    """Extract the embedded TEXT layer of a PDF (digital PDFs). Scanned/image-only PDFs
    have no text layer → typed `⚠️ OCR_PDF_SCANNED_UNAVAILABLE` (true OCR of scanned pages
    is a deferred follow-up; use vlm_query on a page image meanwhile). Reuses the view_image
    local-file trust boundary."""
    fp, err = _resolve_local_file(ctx, path, max_bytes=_OCR_PDF_MAX_BYTES)
    if err:
        return err
    try:
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001
        return "⚠️ OCR_PDF_UNAVAILABLE: the 'pypdf' dependency is not installed in this build."
    try:
        reader = PdfReader(str(fp))
        pages = list(reader.pages)
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ OCR_PDF_UNAVAILABLE: could not parse PDF ({type(exc).__name__})."
    total = len(pages)
    cap = int(max_pages) if int(max_pages or 0) > 0 else _OCR_PDF_MAX_PAGES
    chunks: List[str] = []
    for page in pages[:cap]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 — one bad page must not sink the whole extraction
            chunks.append("")
    text = "\n\n".join(c for c in chunks if c).strip()
    if not text:
        return (
            "⚠️ OCR_PDF_SCANNED_UNAVAILABLE: this PDF has no extractable text layer (likely "
            "scanned/image-only). True OCR of scanned pages is not available in this build — "
            "render a page to an image and call vlm_query on it instead."
        )
    note = "" if total <= cap else f"\n\n[disclosed: showed first {cap} of {total} pages]"
    if len(text) > _OCR_PDF_MAX_CHARS:
        text = text[:_OCR_PDF_MAX_CHARS]
        note += "\n[disclosed: text truncated]"
    return f"PDF text ({min(total, cap)} page(s)):\n\n{text}{note}"


def _youtube_video_id(url: str) -> str:
    """Best-effort extraction of an 11-char YouTube video id from a URL or a bare id."""
    text = str(url or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", text):
        return text
    for pat in (r"[?&]v=([0-9A-Za-z_-]{11})", r"youtu\.be/([0-9A-Za-z_-]{11})", r"/(?:embed|shorts|v)/([0-9A-Za-z_-]{11})"):
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return ""


def _extract_json_array(text: str, key: str) -> str | None:
    """Depth-balanced extraction of the JSON array assigned to `"key":[ ... ]` (handles
    nested arrays like a caption track's `name.runs`, which a non-greedy regex would split)."""
    idx = text.find(f'"{key}":[')
    if idx < 0:
        return None
    start = text.find("[", idx)
    depth = 0
    for j in range(start, len(text)):
        c = text[j]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start : j + 1]
    return None


def _youtube_transcript(ctx: ToolContext, url: str = "", lang: str = "en") -> str:
    """Fetch a YouTube video's caption transcript (timed-text) over plain HTTP. Web-gated
    like web_search (registry._WEB_TOOLS). Returns `⚠️ YOUTUBE_TRANSCRIPT_UNAVAILABLE` when
    captions are absent or the (unofficial, no-SLA) endpoint shape changes — never raises."""
    vid = _youtube_video_id(url)
    if not vid:
        return "⚠️ TOOL_ARG_ERROR (youtube_transcript): provide a YouTube URL or 11-char video id."
    try:
        import requests
    except Exception:  # noqa: BLE001
        return "⚠️ YOUTUBE_TRANSCRIPT_UNAVAILABLE: the 'requests' dependency is not installed."
    try:
        watch = requests.get(f"https://www.youtube.com/watch?v={vid}", headers=_YT_HEADERS, timeout=_YT_HTTP_TIMEOUT_SEC)
        watch.raise_for_status()
        raw = _extract_json_array(watch.text, "captionTracks")
        if not raw:
            return "⚠️ YOUTUBE_TRANSCRIPT_UNAVAILABLE: no caption tracks for this video."
        tracks = _json.loads(raw)
        if not isinstance(tracks, list) or not tracks:
            return "⚠️ YOUTUBE_TRANSCRIPT_UNAVAILABLE: no caption tracks for this video."
        chosen = next((t for t in tracks if isinstance(t, dict) and str(t.get("languageCode") or "").startswith(str(lang or "en"))), tracks[0])
        base_url = str(chosen.get("baseUrl") or "")
        if not base_url:
            return "⚠️ YOUTUBE_TRANSCRIPT_UNAVAILABLE: caption track has no fetch URL."
        xml = requests.get(base_url, headers=_YT_HEADERS, timeout=_YT_HTTP_TIMEOUT_SEC).text
        parts = re.findall(r"<text[^>]*>(.*?)</text>", xml, flags=re.DOTALL)
        text = _html.unescape(re.sub(r"<[^>]+>", "", "\n".join(parts))).strip()
        if not text:
            return "⚠️ YOUTUBE_TRANSCRIPT_UNAVAILABLE: the caption track was empty."
        note = "" if len(text) <= _YT_MAX_CHARS else "\n[disclosed: transcript truncated]"
        return f"YouTube transcript ({vid}, lang={chosen.get('languageCode') or '?'}):\n\n{text[:_YT_MAX_CHARS]}{note}"
    except Exception as exc:  # noqa: BLE001 — unofficial endpoint; fail soft + typed
        return f"⚠️ YOUTUBE_TRANSCRIPT_UNAVAILABLE: fetch failed ({type(exc).__name__})."


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="ocr_pdf",
            schema={
                "name": "ocr_pdf",
                "description": (
                    "Extract the text of a local PDF file (the embedded text layer of a digital PDF). "
                    "Use for reading PDFs attached to the task (see the [ATTACHMENTS] manifest) or produced "
                    "during work. Scanned/image-only PDFs have no text layer and return a typed "
                    "OCR_PDF_SCANNED_UNAVAILABLE notice — for those, render a page and use vlm_query."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Local PDF path (inside the active workspace / task drive / artifact store / attachments / uploads)."},
                        "max_pages": {"type": "integer", "description": f"Cap pages read (default {_OCR_PDF_MAX_PAGES}; over-cap is disclosed)."},
                    },
                    "required": ["path"],
                },
            },
            handler=_ocr_pdf,
            timeout_sec=120,
        ),
        ToolEntry(
            name="youtube_transcript",
            schema={
                "name": "youtube_transcript",
                "description": (
                    "Fetch the caption transcript of a YouTube video by URL or video id. Returns the "
                    "transcript text, or a typed YOUTUBE_TRANSCRIPT_UNAVAILABLE notice when the video has "
                    "no captions. Requires web access."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "YouTube URL or 11-char video id."},
                        "lang": {"type": "string", "description": "Preferred caption language code prefix (default 'en'); falls back to the first available track."},
                    },
                    "required": ["url"],
                },
            },
            handler=_youtube_transcript,
            timeout_sec=90,
        ),
    ]
