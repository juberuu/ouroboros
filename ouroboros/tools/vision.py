"""Vision LLM tools for browser screenshots and uploaded images."""

from __future__ import annotations

import logging
import pathlib
import os
from typing import Any, Dict, List, Tuple

from ouroboros.config import SETTINGS_DEFAULTS, resolve_effort
from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DEFAULT_VLM_MODEL = SETTINGS_DEFAULTS["OUROBOROS_MODEL"]


def _get_llm_client():
    """Lazy-import LLMClient to avoid circular imports."""
    from ouroboros.llm import LLMClient
    return LLMClient()


def _analyze_screenshot(ctx: ToolContext, prompt: str = "Describe what you see in this screenshot. Note any important UI elements, text, errors, or visual issues.", model: str = "") -> str:
    """Analyze the last browser screenshot via VLM."""
    b64 = ctx.browser_state.last_screenshot_b64
    if not b64:
        return (
            "⚠️ No screenshot available. "
            "First call browse_page(output='screenshot') or browser_action(action='screenshot')."
        )

    try:
        client = _get_llm_client()
        vlm_model = _resolve_vlm_model(client, model, ctx=ctx)
        if not vlm_model:
            return _VLM_NO_VISION_MODEL_MSG
        text, usage = client.vision_query(
            prompt=prompt,
            images=[_image_payload_from_base64(b64, "image/png")],
            model=vlm_model,
            reasoning_effort=resolve_effort("task"),
            timeout=_VLM_HTTP_TIMEOUT_SEC,
        )

        _emit_usage(ctx, usage, vlm_model)

        return text or "(no response from VLM)"
    except Exception as e:
        log.warning("analyze_screenshot failed: %s", e, exc_info=True)
        return f"⚠️ VLM_ANALYSIS_FAILED: {e}"


_IMAGE_MAGIC: List[tuple] = [
    (b'\x89PNG\r\n\x1a\n', "image/png"),
    (b'\xff\xd8\xff', "image/jpeg"),
    (b'GIF87a', "image/gif"),
    (b'GIF89a', "image/gif"),
]
_IMAGE_WEBP_MAGIC = (b'RIFF', b'WEBP')
_VLM_MAX_FILE_BYTES = 20 * 1024 * 1024
_VLM_MAX_PROVIDER_BYTES = 6 * 1024 * 1024
_VLM_MAX_IMAGE_SIDE = 1600
_VLM_HTTP_TIMEOUT_SEC = 90.0


def _path_is_under(path: "pathlib.Path", root: "pathlib.Path") -> bool:
    """Return True if a resolved path is root itself or a descendant."""
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _detect_image_mime_for_vlm(raw: bytes) -> str:
    """Return MIME type string or empty string if not a recognised image."""
    for magic, mime in _IMAGE_MAGIC:
        if raw[:len(magic)] == magic:
            return mime
    if raw[:4] == _IMAGE_WEBP_MAGIC[0] and raw[8:12] == _IMAGE_WEBP_MAGIC[1]:
        return "image/webp"
    return ""


def _downscale_image_for_vlm(raw: bytes, mime: str) -> Tuple[bytes, str]:
    """Cap very large image payloads before sending them to the VLM provider."""
    if len(raw) <= _VLM_MAX_PROVIDER_BYTES:
        try:
            from PIL import Image
            import io

            with Image.open(io.BytesIO(raw)) as img:
                if max(img.size) <= _VLM_MAX_IMAGE_SIDE:
                    return raw, mime
        except Exception:
            return raw, mime

    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            if img.mode != "RGB":
                background = Image.new("RGB", img.size, (255, 255, 255))
                alpha = img.getchannel("A") if img.mode in {"RGBA", "LA"} else None
                background.paste(img.convert("RGB"), mask=alpha)
                img = background
            else:
                img = img.copy()
            max_side = min(_VLM_MAX_IMAGE_SIDE, max(img.size))
            for quality in (85, 75, 65, 55):
                candidate = img.copy()
                candidate.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                out = io.BytesIO()
                candidate.save(out, format="JPEG", quality=quality, optimize=True)
                data = out.getvalue()
                if len(data) <= _VLM_MAX_PROVIDER_BYTES:
                    return data, "image/jpeg"
                max_side = max(64, int(max_side * 0.75))
    except Exception:
        log.debug("Failed to downscale VLM image payload", exc_info=True)
    if len(raw) <= _VLM_MAX_PROVIDER_BYTES:
        return raw, mime
    raise ValueError(
        f"⚠️ VLM_IMAGE_TOO_LARGE: image payload exceeds {int(_VLM_MAX_PROVIDER_BYTES / 1024 / 1024)}MB provider cap"
    )


def _image_payload_from_bytes(raw: bytes, mime: str) -> Dict[str, str]:
    import base64

    capped_raw, capped_mime = _downscale_image_for_vlm(raw, mime)
    return {"base64": base64.b64encode(capped_raw).decode(), "mime": capped_mime}


def _image_payload_from_base64(image_base64: str, mime: str) -> Dict[str, str]:
    import base64

    try:
        raw = base64.b64decode(image_base64, validate=True)
    except Exception:
        return {"base64": image_base64, "mime": mime}
    return _image_payload_from_bytes(raw, mime)


_VLM_NO_VISION_MODEL_MSG = (
    "⚠️ VLM_NO_VISION_MODEL: image analysis is unavailable — neither the active "
    "model nor any configured vision slot (light/code/main/fallback) accepts image "
    "input. Do NOT retry the image. Instead inspect the page as TEXT/DOM "
    "(browse_page output='html' or 'text') and the console/network for errors, or "
    "switch_model to a vision-capable model, or ask the owner to configure one."
)


def _vision_capable_slot_candidates(client: Any, ctx: Any = None) -> List[str]:
    """Configured models that may serve a VLM sub-call, most-local/cheapest first
    (active task model -> light -> code -> main -> fallback). Reviewer/scope slots
    are deliberately NOT poached. De-duplicated, order-preserving, empties dropped."""
    out: List[str] = [
        str(getattr(ctx, "active_model", "") or getattr(ctx, "task_model_override", "") or "").strip(),
    ]
    try:
        from ouroboros.config import get_light_model
        out.append(str(get_light_model() or "").strip())
    except Exception:
        pass
    out.append(str(os.environ.get("OUROBOROS_MODEL_CODE", "") or "").strip())
    try:
        out.append(str(client.default_model() or "").strip())
    except Exception:
        pass
    out.append(str(os.environ.get("OUROBOROS_MODEL", "") or "").strip())
    out.append(str(os.environ.get("OUROBOROS_MODEL_FALLBACK", "") or "").strip())
    seen: set = set()
    uniq: List[str] = []
    for model in out:
        if model and model not in seen:
            seen.add(model)
            uniq.append(model)
    return uniq


def _resolve_vlm_model(client: Any, requested_model: str = "", *, ctx: Any = None) -> str:
    """Resolve a VISION-CAPABLE model for an image sub-call, or "" when none is
    available. An explicit requested model is honored ONLY if it actually supports
    vision (else "" -> the caller surfaces a typed capability gap, never a blind 404
    that the loop then bangs on). Otherwise route to the first vision-capable
    configured slot (active -> light -> code -> main -> fallback) — gemini light/code
    are vision-capable, so this usually succeeds without any new model slot."""
    from ouroboros.provider_models import supports_vision
    requested = str(requested_model or "").strip()
    if requested:
        return requested if supports_vision(requested) else ""
    for candidate in _vision_capable_slot_candidates(client, ctx):
        if supports_vision(candidate):
            return candidate
    return ""


def _allowed_file_roots(ctx: Any = None) -> List["pathlib.Path"]:
    """Roots a VLM file_path may be read from: the uploads dir PLUS — same trust
    boundary the agent already has via read_file/run_command — the ACTIVE task
    workspace, so it can analyze a screenshot it just produced. Never arbitrary
    filesystem paths (no exfiltration surface the agent doesn't already hold)."""
    import pathlib
    data_dir = os.environ.get("OUROBOROS_DATA_DIR", "")
    if data_dir:
        roots = [pathlib.Path(data_dir).expanduser().resolve() / "uploads"]
    else:
        roots = [pathlib.Path("~/Ouroboros/data/uploads").expanduser().resolve()]
    if ctx is not None:
        try:
            from ouroboros.tools.registry import active_repo_dir_for
            roots.append(pathlib.Path(active_repo_dir_for(ctx)).expanduser().resolve())
        except Exception:
            pass
        # C3: the active task's first-class artifact roots (artifact_store +
        # task_drive) are the SAME trust boundary the agent already holds via
        # read_file/run_command — so a screenshot it just registered as an artifact
        # is readable too. Never arbitrary paths (no new exfiltration surface).
        for _root in ("artifact_store", "task_drive"):
            try:
                from ouroboros.tool_access import resource_root_path
                roots.append(pathlib.Path(resource_root_path(ctx, _root)).expanduser().resolve())
            except Exception:
                pass
    return roots


def _vlm_query(ctx: ToolContext, prompt: str, image_url: str = "", image_base64: str = "", image_mime: str = "image/png", file_path: str = "", model: str = "") -> str:
    """Analyze one image from uploads file_path, public URL, or base64."""
    if not image_url and not image_base64 and not file_path:
        return "⚠️ Provide one of: file_path, image_url, or image_base64."

    images: List[Dict[str, Any]] = []
    try:
        if file_path:
            import pathlib
            fp = pathlib.Path(file_path).expanduser().resolve()
            if not fp.exists():
                return f"⚠️ File not found: {file_path}"
            allowed = _allowed_file_roots(ctx)
            if not any(_path_is_under(fp, root) for root in allowed):
                return (
                    f"⚠️ file_path must be inside the uploads directory or the active task "
                    f"workspace. Resolved path: {fp}. Use send_photo or read_file for other paths."
                )
            # Honor the task protected-artifact policy: a workspace file may still be
            # a black-box protected artifact whose bytes must not be read (same
            # contract as read_file / query_code — protected_artifacts.block_reason_
            # for_path with operation "read_bytes"). Without this, vlm_query would be
            # a read_bytes bypass of task_contract.resource_policy.
            try:
                from ouroboros.protected_artifacts import block_reason_for_path
                _artifact_block = block_reason_for_path(ctx, fp, "read_bytes")
            except Exception:
                _artifact_block = ""
            if _artifact_block:
                return _artifact_block
            if fp.stat().st_size > _VLM_MAX_FILE_BYTES:
                return f"⚠️ File too large ({fp.stat().st_size} bytes). Max {_VLM_MAX_FILE_BYTES} bytes."
            try:
                raw = fp.read_bytes()
            except Exception as e:
                return f"⚠️ Failed to read image file: {e}"
            # Fail closed: only recognized image bytes may reach the VLM.
            mime = _detect_image_mime_for_vlm(raw)
            if not mime:
                return (
                    "⚠️ File does not appear to be a supported image (PNG/JPEG/GIF/WEBP). "
                    "Only image files may be sent to the VLM via file_path."
                )
            images.append(_image_payload_from_bytes(raw, mime))
        elif image_url:
            images.append({"url": image_url})
        else:
            images.append(_image_payload_from_base64(image_base64, image_mime))

        client = _get_llm_client()
        vlm_model = _resolve_vlm_model(client, model, ctx=ctx)
        if not vlm_model:
            return _VLM_NO_VISION_MODEL_MSG
        text, usage = client.vision_query(
            prompt=prompt,
            images=images,
            model=vlm_model,
            reasoning_effort=resolve_effort("task"),
            timeout=_VLM_HTTP_TIMEOUT_SEC,
        )

        _emit_usage(ctx, usage, vlm_model)

        return text or "(no response from VLM)"
    except Exception as e:
        log.warning("vlm_query failed: %s", e, exc_info=True)
        return f"⚠️ VLM_QUERY_FAILED: {e}"


def _emit_usage(ctx: ToolContext, usage: Dict[str, Any], model: str) -> None:
    """Emit LLM usage event for budget tracking."""
    if ctx.event_queue is None:
        return
    try:
        event = {
            "type": "llm_usage",
            "model": model,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cached_tokens": usage.get("cached_tokens", 0),
            "cost": usage.get("cost", 0.0),
            "task_id": ctx.task_id,
            "task_type": ctx.current_task_type or "task",
        }
        ctx.event_queue.put_nowait(event)
    except Exception:
        log.debug("Failed to emit VLM usage event", exc_info=True)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="analyze_screenshot",
            schema={
                "name": "analyze_screenshot",
                "description": (
                    "Analyze the last browser screenshot using a Vision LLM. "
                    "Must call browse_page(output='screenshot') or browser_action(action='screenshot') first. "
                    "Returns a text description and analysis of the screenshot. "
                    "Use this to verify UI, check for visual errors, or understand page layout."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "What to look for or analyze in the screenshot (default: general description)",
                        },
                        "model": {
                            "type": "string",
                            "description": "VLM model to use (default: current OUROBOROS_MODEL)",
                        },
                    },
                    "required": [],
                },
            },
            handler=_analyze_screenshot,
            timeout_sec=90,
        ),
        ToolEntry(
            name="vlm_query",
            schema={
                "name": "vlm_query",
                "description": (
                    "Analyze any image using a Vision LLM. "
                    "Provide one of: file_path (local file, preferred — avoids large base64 in arguments), "
                    "image_url (public URL), or image_base64 (base64-encoded PNG/JPEG). "
                    "Use file_path for files already on disk (e.g. data/uploads/ attachments). "
                    "Use for: analyzing charts, reading diagrams, understanding screenshots, checking UI."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "What to analyze or describe about the image",
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Local file path to image (preferred — reads from disk, avoids base64 in arguments). Must be inside the uploads directory (data/uploads/) or the active task workspace.",
                        },
                        "image_url": {
                            "type": "string",
                            "description": "Public URL of the image to analyze",
                        },
                        "image_base64": {
                            "type": "string",
                            "description": "Base64-encoded image data",
                        },
                        "image_mime": {
                            "type": "string",
                            "description": "MIME type for base64 image (default: image/png)",
                        },
                        "model": {
                            "type": "string",
                            "description": "VLM model to use (default: current OUROBOROS_MODEL)",
                        },
                    },
                    "required": ["prompt"],
                },
            },
            handler=_vlm_query,
            timeout_sec=90,
        ),
    ]
