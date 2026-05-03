"""File browser API endpoints extracted from server.py."""

from __future__ import annotations

import logging
import mimetypes
import os
import pathlib
import shutil
from contextlib import suppress
from typing import Any
from urllib.parse import quote

log = logging.getLogger(__name__)

from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from ouroboros.server_auth import is_loopback_host
from ouroboros.utils import safe_relpath

_FILE_BROWSER_MAX_DIR_ENTRIES = 500
_FILE_BROWSER_MAX_READ_BYTES = 256 * 1024
_FILE_BROWSER_MAX_PREVIEW_CHARS = 120_000
_FILE_BROWSER_UPLOAD_CHUNK_SIZE = 1024 * 1024
_FILE_BROWSER_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_IMAGE_PREVIEW_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_PDF_PREVIEW_EXTENSIONS = {".pdf"}
_TEXT_PREVIEW_EXTENSIONS = {
    ".py", ".md", ".txt", ".json", ".jsonl", ".toml", ".yml", ".yaml",
    ".js", ".css", ".html", ".ts", ".tsx", ".jsx", ".ini", ".cfg",
    ".sh", ".zsh", ".bash", ".ps1", ".env", ".xml", ".csv",
}
_SKILL_OWNER_STATE_FILENAMES = frozenset({
    "enabled.json",
    "grants.json",
    "review.json",
    "clawhub.json",
    "deps.json",
})


def _is_skill_owner_state_target(target: pathlib.Path) -> bool:
    if target.name.lower() not in _SKILL_OWNER_STATE_FILENAMES:
        return False
    from ouroboros import config as _cfg
    data_root = pathlib.Path(_cfg.DATA_DIR).resolve(strict=False)
    try:
        rel = target.relative_to(data_root)
        parts = rel.parts
        if (
            len(parts) == 4
            and parts[0].lower() == "state"
            and parts[1].lower() == "skills"
        ):
            return True
    except (OSError, ValueError):
        pass
    try:
        rel = target.resolve(strict=False).relative_to(data_root)
        parts = rel.parts
        if (
            len(parts) == 4
            and parts[0].lower() == "state"
            and parts[1].lower() == "skills"
        ):
            return True
    except (OSError, ValueError):
        pass
    skills_state_root = data_root / "state" / "skills"
    if not skills_state_root.is_dir():
        return False
    try:
        target_parent = target.parent.resolve(strict=False)
    except OSError:
        return False
    for skill_state_dir in skills_state_root.iterdir():
        try:
            if skill_state_dir.resolve(strict=False) == target_parent:
                return True
        except OSError:
            continue
    return False


class FileBrowserPayloadTooLarge(ValueError):
    """Raised when an upload exceeds the configured limit."""


def _request_is_local(request: Request) -> bool:
    host = request.client.host if request.client else None
    return is_loopback_host(host)


def _normalize_root(raw: str) -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(os.path.expandvars(raw))).resolve()


def _configured_root_text() -> str:
    return (os.environ.get("OUROBOROS_FILE_BROWSER_DEFAULT", "") or "").strip()


def _get_file_browser_root(request: Request) -> pathlib.Path:
    raw = _configured_root_text()
    local_request = _request_is_local(request)
    if not raw:
        if local_request:
            return pathlib.Path.home().resolve()
        raise ValueError(
            "OUROBOROS_FILE_BROWSER_DEFAULT must point to an existing directory "
            "when the server is accessed over network."
        )

    root_dir = _normalize_root(raw)
    if root_dir.exists() and root_dir.is_dir():
        return root_dir
    if local_request:
        return pathlib.Path.home().resolve()
    raise ValueError(f"Configured file browser root does not exist: {root_dir}")


def _resolve_target(request: Request, rel_path: str) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    root_dir = _get_file_browser_root(request)
    requested = root_dir / safe_relpath(rel_path or ".")
    try:
        requested.relative_to(root_dir)
    except ValueError as exc:
        raise ValueError("Path escapes file browser root.") from exc
    resolved = requested.resolve(strict=False)
    return root_dir, requested, resolved


def _is_owner_only_settings_file(target: pathlib.Path) -> bool:
    """v5.1.2 iter-2 real triad+scope finding SR2: the Files API writes
    /deletes/transfers/uploads bypass ``ouroboros.tools.core._data_write``
    and the ``save_settings`` chokepoint, so they could overwrite
    ``data/settings.json`` directly. Compare via ``Path.samefile`` for
    inode-aware semantics (handles macOS APFS / Windows NTFS case-folding
    + symlinks); fall back to a same-parent + case-insensitive name match
    for not-yet-existing target paths.
    """
    from ouroboros import config as _cfg
    settings_path = pathlib.Path(_cfg.SETTINGS_PATH)
    try:
        if target.exists() and settings_path.exists():
            if target.samefile(settings_path):
                return True
    except OSError:
        pass
    try:
        if target.parent.resolve() == settings_path.parent.resolve():
            if target.name.lower() == settings_path.name.lower():
                return True
    except OSError:
        pass
    return False


def _is_owner_only_file(target: pathlib.Path) -> bool:
    if _is_owner_only_settings_file(target):
        return True
    if _is_skill_owner_state_target(target):
        return True
    from ouroboros import config as _cfg
    data_root = pathlib.Path(_cfg.DATA_DIR).resolve(strict=False)
    skills_state_root = pathlib.Path(_cfg.DATA_DIR) / "state" / "skills"
    if target.exists() and skills_state_root.is_dir():
        for owner_state_file in skills_state_root.glob("*/*"):
            if owner_state_file.name.lower() not in _SKILL_OWNER_STATE_FILENAMES:
                continue
            try:
                if owner_state_file.exists() and target.samefile(owner_state_file):
                    return True
            except OSError:
                continue
    try:
        rel = target.resolve(strict=False).relative_to(data_root)
    except (OSError, ValueError):
        return False
    parts = rel.parts
    return (
        len(parts) == 4
        and parts[0].lower() == "state"
        and parts[1].lower() == "skills"
        and parts[3].lower() in _SKILL_OWNER_STATE_FILENAMES
    )


def _contains_owner_only_file(target: pathlib.Path) -> bool:
    if _is_owner_only_file(target):
        return True
    if not target.is_dir():
        return False
    try:
        for child in target.rglob("*"):
            if _is_owner_only_file(child):
                return True
    except OSError:
        return False
    return False


def _is_skill_control_plane_api_target(target: pathlib.Path) -> bool:
    """Return True for skill payload provenance / launcher sidecars.

    File Browser endpoints bypass ``tools/core._data_write`` and touch the
    filesystem directly, so every mutating route must re-apply the same
    control-plane guard. This wrapper keeps import/config errors best-effort:
    unrelated file-browser paths should not brick if config is unavailable
    during early startup.
    """
    try:
        from ouroboros.config import DATA_DIR
        from ouroboros.tools.core import is_skill_control_plane_path

        data_root = pathlib.Path(DATA_DIR).resolve(strict=False)
        return is_skill_control_plane_path(pathlib.Path(target), data_root)
    except Exception:
        log.debug("control-plane guard probe failed in file_browser_api", exc_info=True)
        return False


def _contains_skill_control_plane_file(target: pathlib.Path) -> bool:
    """Recursive version of ``_is_skill_control_plane_api_target``.

    Used for directory delete/transfer so a whole payload directory cannot be
    removed or copied/moved through the generic file browser while it contains
    provenance/seed/deps control-plane files.
    """
    if _is_skill_control_plane_api_target(target):
        return True
    if not target.is_dir():
        return False
    try:
        for child in target.rglob("*"):
            if _is_skill_control_plane_api_target(child):
                return True
    except OSError:
        return False
    return False


_CONTROL_PLANE_FILES_API_ERROR = JSONResponse(
    {
        "error": (
            "Refusing to modify skill provenance / launcher seed marker "
            "(.clawhub.json, .ouroboroshub.json, SKILL.openclaw.md, .seed-origin). "
            "Use marketplace Uninstall/Update flows or edit user-authored payload files instead."
        ),
    },
    status_code=400,
)


# Standard error response for the Files API guard. Matches the wording
# of ``tools/core.py::_data_write`` so the operator hears one consistent
# message regardless of which surface they used.
_OWNER_ONLY_FILES_API_ERROR = JSONResponse(
    {
        "error": (
            "settings.json and skill review/enablement/grant/provenance state "
            "cannot be modified through the Files API. Owner-controlled values "
            "(OUROBOROS_RUNTIME_MODE, credentials, A2A bind/expose, review "
            "enforcement) are not agent-mutable. Stop the agent, edit "
            "~/Ouroboros/data/settings.json directly, then restart."
        ),
    },
    status_code=403,
)


def _format_path(root_dir: pathlib.Path, rel_path: str) -> str:
    rel = rel_path or "."
    return str(root_dir) if rel in {"", "."} else str(root_dir / rel)


def _read_prefix(path: pathlib.Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(limit)


def _guess_text_file(path: pathlib.Path) -> bool:
    if path.suffix.lower() in _TEXT_PREVIEW_EXTENSIONS:
        return True
    try:
        sample = _read_prefix(path, 4096)
    except Exception:
        return False
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _sanitize_upload_filename(filename: str) -> str:
    raw = (filename or "").replace("\\", "/").strip()
    name = pathlib.PurePosixPath(raw).name.strip()
    if not name or name in {".", ".."}:
        raise ValueError("Invalid filename.")
    if "/" in name:
        raise ValueError("Filenames must not contain path separators.")
    return name


def _guess_media_type(path: pathlib.Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _entry_within_root(entry: pathlib.Path, root_dir: pathlib.Path) -> bool:
    try:
        entry.relative_to(root_dir)
        return True
    except Exception:
        return False


def _copy_path(source: pathlib.Path, destination: pathlib.Path) -> None:
    if source.is_symlink():
        destination.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
        return
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
        return
    shutil.copy2(source, destination)


def _relative_path(root_dir: pathlib.Path, path: pathlib.Path) -> str:
    return path.relative_to(root_dir).as_posix() or "."


async def api_files_list(request: Request) -> JSONResponse:
    rel_path = request.query_params.get("path") or "."
    try:
        root_dir, target, _ = _resolve_target(request, rel_path)
        if not target.exists():
            return JSONResponse({"error": f"Path not found: {rel_path}"}, status_code=404)
        if not target.is_dir():
            return JSONResponse({"error": f"Not a directory: {rel_path}"}, status_code=400)

        entries: list[dict[str, Any]] = []
        visible_entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        for entry in visible_entries:
            if len(entries) >= _FILE_BROWSER_MAX_DIR_ENTRIES:
                break
            if not _entry_within_root(entry, root_dir):
                continue
            item: dict[str, Any] = {
                "name": entry.name,
                "path": _relative_path(root_dir, entry),
                "type": "dir" if entry.is_dir() else "file",
                "is_symlink": entry.is_symlink(),
            }
            if entry.is_file():
                try:
                    item["size"] = int(entry.stat().st_size)
                except Exception:
                    item["size"] = None
            entries.append(item)

        target_rel = _relative_path(root_dir, target)
        parts = [] if target_rel == "." else [part for part in target_rel.split("/") if part]
        breadcrumb = [{"name": str(root_dir), "path": "."}]
        accum: list[str] = []
        for part in parts:
            accum.append(part)
            breadcrumb.append({"name": part, "path": "/".join(accum)})

        parent_path = "."
        if target_rel != ".":
            parent_path = "/".join(parts[:-1]) if len(parts) > 1 else "."

        return JSONResponse({
            "root_path": str(root_dir),
            "path": target_rel,
            "display_path": _format_path(root_dir, target_rel),
            "parent_path": parent_path,
            "breadcrumb": breadcrumb,
            "entries": entries,
            "truncated": len(visible_entries) > len(entries) or len(entries) >= _FILE_BROWSER_MAX_DIR_ENTRIES,
            "default_path": ".",
            "default_display_path": str(root_dir),
        })
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_files_read(request: Request) -> JSONResponse:
    rel_path = request.query_params.get("path", "")
    try:
        if not rel_path:
            return JSONResponse({"error": "Missing path."}, status_code=400)
        root_dir, target, _ = _resolve_target(request, rel_path)
        if not target.exists():
            return JSONResponse({"error": f"Path not found: {rel_path}"}, status_code=404)
        if not target.is_file():
            return JSONResponse({"error": f"Not a file: {rel_path}"}, status_code=400)

        size = int(target.stat().st_size)
        rel = _relative_path(root_dir, target)
        if target.suffix.lower() in _IMAGE_PREVIEW_EXTENSIONS:
            encoded_rel = quote(rel, safe="/")
            return JSONResponse({
                "root_path": str(root_dir),
                "path": rel,
                "display_path": _format_path(root_dir, rel),
                "name": target.name,
                "size": size,
                "is_text": False,
                "is_image": True,
                "is_pdf": False,
                "media_type": _guess_media_type(target),
                "content_url": f"/api/files/content?path={encoded_rel}",
                "content": "",
                "truncated": False,
            })
        if target.suffix.lower() in _PDF_PREVIEW_EXTENSIONS:
            encoded_rel = quote(rel, safe="/")
            return JSONResponse({
                "root_path": str(root_dir),
                "path": rel,
                "display_path": _format_path(root_dir, rel),
                "name": target.name,
                "size": size,
                "is_text": False,
                "is_image": False,
                "is_pdf": True,
                "media_type": "application/pdf",
                "content_url": f"/api/files/content?path={encoded_rel}",
                "content": "",
                "truncated": False,
            })
        if not _guess_text_file(target):
            return JSONResponse({
                "root_path": str(root_dir),
                "path": rel,
                "display_path": _format_path(root_dir, rel),
                "name": target.name,
                "size": size,
                "is_text": False,
                "is_image": False,
                "is_pdf": False,
                "content": "",
                "truncated": False,
            })

        raw = _read_prefix(target, _FILE_BROWSER_MAX_READ_BYTES + 1)
        truncated = len(raw) > _FILE_BROWSER_MAX_READ_BYTES or size > _FILE_BROWSER_MAX_READ_BYTES
        text = raw[:_FILE_BROWSER_MAX_READ_BYTES].decode("utf-8", errors="replace")
        if len(text) > _FILE_BROWSER_MAX_PREVIEW_CHARS:
            text = text[:_FILE_BROWSER_MAX_PREVIEW_CHARS]
            truncated = True

        return JSONResponse({
            "root_path": str(root_dir),
            "path": rel,
            "display_path": _format_path(root_dir, rel),
            "name": target.name,
            "size": size,
            "is_text": True,
            "is_image": False,
            "is_pdf": False,
            "content": text,
            "truncated": truncated,
        })
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_files_download(request: Request) -> FileResponse | JSONResponse:
    rel_path = request.query_params.get("path", "")
    try:
        if not rel_path:
            return JSONResponse({"error": "Missing path."}, status_code=400)
        _, target, _ = _resolve_target(request, rel_path)
        if not target.exists():
            return JSONResponse({"error": f"Path not found: {rel_path}"}, status_code=404)
        if not target.is_file():
            return JSONResponse({"error": f"Not a file: {rel_path}"}, status_code=400)
        return FileResponse(str(target), filename=target.name)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_files_content(request: Request) -> FileResponse | JSONResponse:
    rel_path = request.query_params.get("path", "")
    try:
        if not rel_path:
            return JSONResponse({"error": "Missing path."}, status_code=400)
        _, target, _ = _resolve_target(request, rel_path)
        if not target.exists():
            return JSONResponse({"error": f"Path not found: {rel_path}"}, status_code=404)
        if not target.is_file():
            return JSONResponse({"error": f"Not a file: {rel_path}"}, status_code=400)
        return FileResponse(str(target), media_type=_guess_media_type(target))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_files_write(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON payload."}, status_code=400)

    try:
        rel_path = str(payload.get("path") or "").strip()
        if not rel_path:
            return JSONResponse({"error": "Missing path."}, status_code=400)
        if "content" not in payload:
            return JSONResponse({"error": "Missing content."}, status_code=400)

        content = str(payload.get("content"))
        create = bool(payload.get("create"))
        root_dir, target, _ = _resolve_target(request, rel_path)
        if _contains_owner_only_file(target):
            return _OWNER_ONLY_FILES_API_ERROR
        if _contains_skill_control_plane_file(target):
            return _CONTROL_PLANE_FILES_API_ERROR
        if not target.exists():
            if not create:
                return JSONResponse({"error": f"Path not found: {rel_path}"}, status_code=404)
            if not target.parent.exists():
                return JSONResponse({"error": f"Parent directory not found: {target.parent}"}, status_code=404)
            if not target.parent.is_dir():
                return JSONResponse({"error": "Parent path is not a directory."}, status_code=400)
            tmp_target = target.with_name(f".{target.name}.editing")
            try:
                tmp_target.write_text(content, encoding="utf-8")
                tmp_target.replace(target)
            finally:
                if tmp_target.exists():
                    with suppress(Exception):
                        tmp_target.unlink()
            return JSONResponse({
                "ok": True,
                "created": True,
                "path": _relative_path(root_dir, target),
                "display_path": _format_path(root_dir, _relative_path(root_dir, target)),
                "name": target.name,
                "size": int(target.stat().st_size),
            })

        if not target.is_file():
            return JSONResponse({"error": f"Not a file: {rel_path}"}, status_code=400)
        if target.suffix.lower() in _IMAGE_PREVIEW_EXTENSIONS or not _guess_text_file(target):
            return JSONResponse({"error": "Only text files can be edited in the browser."}, status_code=400)

        if target.is_symlink():
            target.write_text(content, encoding="utf-8")
        else:
            tmp_target = target.with_name(f".{target.name}.editing")
            try:
                tmp_target.write_text(content, encoding="utf-8")
                tmp_target.replace(target)
            finally:
                if tmp_target.exists():
                    with suppress(Exception):
                        tmp_target.unlink()

        rel = _relative_path(root_dir, target)
        return JSONResponse({
            "ok": True,
            "path": rel,
            "display_path": _format_path(root_dir, rel),
            "name": target.name,
            "size": int(target.stat().st_size),
        })
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_files_mkdir(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON payload."}, status_code=400)

    try:
        rel_dir = str(payload.get("path") or ".").strip() or "."
        name = _sanitize_upload_filename(str(payload.get("name") or ""))
        root_dir, target_dir, _ = _resolve_target(request, rel_dir)
        if not target_dir.exists():
            return JSONResponse({"error": f"Path not found: {rel_dir}"}, status_code=404)
        if not target_dir.is_dir():
            return JSONResponse({"error": f"Not a directory: {rel_dir}"}, status_code=400)

        destination = target_dir / name
        if _is_owner_only_file(destination):
            return _OWNER_ONLY_FILES_API_ERROR
        if _is_skill_control_plane_api_target(destination):
            return _CONTROL_PLANE_FILES_API_ERROR
        if destination.exists():
            return JSONResponse({"error": f"Path already exists: {name}"}, status_code=409)
        destination.mkdir(parents=False, exist_ok=False)

        rel = _relative_path(root_dir, destination)
        return JSONResponse({
            "ok": True,
            "path": rel,
            "display_path": _format_path(root_dir, rel),
            "name": destination.name,
            "type": "dir",
        })
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_files_delete(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON payload."}, status_code=400)

    try:
        rel_path = str(payload.get("path") or "").strip()
        if not rel_path:
            return JSONResponse({"error": "Missing path."}, status_code=400)

        root_dir, target, _ = _resolve_target(request, rel_path)
        if target == root_dir:
            return JSONResponse({"error": "Refusing to delete the configured root directory."}, status_code=400)
        if _contains_owner_only_file(target):
            return _OWNER_ONLY_FILES_API_ERROR
        if _contains_skill_control_plane_file(target):
            return _CONTROL_PLANE_FILES_API_ERROR
        if not target.exists():
            return JSONResponse({"error": f"Path not found: {rel_path}"}, status_code=404)

        rel = _relative_path(root_dir, target)
        if target.is_symlink():
            target.unlink()
            deleted_type = "symlink"
        elif target.is_file():
            target.unlink()
            deleted_type = "file"
        elif target.is_dir():
            shutil.rmtree(target)
            deleted_type = "dir"
        else:
            return JSONResponse({"error": f"Unsupported path type: {rel_path}"}, status_code=400)

        return JSONResponse({"ok": True, "path": rel, "type": deleted_type})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_files_transfer(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON payload."}, status_code=400)

    try:
        source_rel = str(payload.get("source_path") or "").strip()
        dest_rel = str(payload.get("destination_dir") or ".").strip() or "."
        mode = str(payload.get("mode") or "copy").strip().lower()
        if not source_rel:
            return JSONResponse({"error": "Missing source_path."}, status_code=400)
        if mode not in {"copy", "move"}:
            return JSONResponse({"error": "Invalid mode. Expected copy or move."}, status_code=400)

        root_dir, source, _ = _resolve_target(request, source_rel)
        _, dest_dir, _ = _resolve_target(request, dest_rel)
        if source == root_dir:
            return JSONResponse({"error": "Refusing to move or copy the configured root directory."}, status_code=400)
        # v5.1.2 iter-2 SR2: refuse if EITHER the source OR the destination
        # resolves to the owner-only settings.json. Source = "move"
        # (deletes settings.json from its expected location), destination
        # = "overwrite settings.json with arbitrary content".
        if _contains_owner_only_file(source):
            return _OWNER_ONLY_FILES_API_ERROR
        if _contains_skill_control_plane_file(source):
            return _CONTROL_PLANE_FILES_API_ERROR
        destination_check = dest_dir / source.name
        if _is_owner_only_file(destination_check):
            return _OWNER_ONLY_FILES_API_ERROR
        if _is_skill_control_plane_api_target(destination_check):
            return _CONTROL_PLANE_FILES_API_ERROR
        if source.is_dir():
            try:
                for child in source.rglob("*"):
                    projected = destination_check / child.relative_to(source)
                    if _is_owner_only_file(projected):
                        return _OWNER_ONLY_FILES_API_ERROR
                    if _is_skill_control_plane_api_target(projected):
                        return _CONTROL_PLANE_FILES_API_ERROR
                    if child.is_symlink():
                        try:
                            resolved = child.resolve(strict=True)
                        except OSError:
                            continue
                        if resolved.is_dir():
                            for linked_child in resolved.rglob("*"):
                                if _is_owner_only_file(projected / linked_child.relative_to(resolved)):
                                    return _OWNER_ONLY_FILES_API_ERROR
                                if _is_skill_control_plane_api_target(projected / linked_child.relative_to(resolved)):
                                    return _CONTROL_PLANE_FILES_API_ERROR
            except OSError:
                pass
        elif _is_owner_only_file(destination_check):
            return _OWNER_ONLY_FILES_API_ERROR
        elif _is_skill_control_plane_api_target(destination_check):
            return _CONTROL_PLANE_FILES_API_ERROR
        if not source.exists():
            return JSONResponse({"error": f"Path not found: {source_rel}"}, status_code=404)
        if not dest_dir.exists():
            return JSONResponse({"error": f"Path not found: {dest_rel}"}, status_code=404)
        if not dest_dir.is_dir():
            return JSONResponse({"error": f"Not a directory: {dest_rel}"}, status_code=400)

        destination = dest_dir / source.name
        if destination.exists():
            return JSONResponse({"error": f"Path already exists: {destination.name}"}, status_code=409)
        try:
            destination.relative_to(root_dir)
        except ValueError:
            return JSONResponse({"error": "Destination escapes file browser root."}, status_code=400)

        if source.is_dir() and not source.is_symlink():
            try:
                destination.relative_to(source)
            except ValueError:
                pass
            else:
                return JSONResponse({"error": "Cannot move or copy a directory into itself."}, status_code=400)

        if mode == "copy":
            _copy_path(source, destination)
        else:
            shutil.move(str(source), str(destination))

        rel = _relative_path(root_dir, destination)
        return JSONResponse({
            "ok": True,
            "mode": mode,
            "path": rel,
            "display_path": _format_path(root_dir, rel),
            "name": destination.name,
            "type": "dir" if destination.is_dir() else "file",
        })
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_files_upload(request: Request) -> JSONResponse:
    try:
        form = await request.form()
        rel_dir = str(form.get("path") or ".")
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            return JSONResponse({"error": "Missing file upload."}, status_code=400)

        root_dir, target_dir, _ = _resolve_target(request, rel_dir)
        if not target_dir.exists():
            return JSONResponse({"error": f"Path not found: {rel_dir}"}, status_code=404)
        if not target_dir.is_dir():
            return JSONResponse({"error": f"Not a directory: {rel_dir}"}, status_code=400)

        filename = _sanitize_upload_filename(upload.filename or "")
        destination = target_dir / filename
        # v5.1.2 iter-2 SR2: an upload's destination would clobber any
        # existing file at that name, including settings.json. Refuse
        # uploads that resolve to the owner-only settings file.
        if _is_owner_only_file(destination):
            return _OWNER_ONLY_FILES_API_ERROR
        if _is_skill_control_plane_api_target(destination):
            return _CONTROL_PLANE_FILES_API_ERROR
        if destination.exists():
            return JSONResponse({"error": f"File already exists: {filename}"}, status_code=409)

        tmp_destination = destination.with_name(f".{destination.name}.uploading")
        bytes_written = 0
        try:
            with tmp_destination.open("wb") as handle:
                while True:
                    chunk = await upload.read(_FILE_BROWSER_UPLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    bytes_written += len(chunk)
                    if bytes_written > _FILE_BROWSER_MAX_UPLOAD_BYTES:
                        raise FileBrowserPayloadTooLarge(
                            f"Upload exceeds {_FILE_BROWSER_MAX_UPLOAD_BYTES} bytes."
                        )
                    handle.write(chunk)
            tmp_destination.replace(destination)
        finally:
            await upload.close()
            if tmp_destination.exists():
                with suppress(Exception):
                    tmp_destination.unlink()

        rel = _relative_path(root_dir, destination)
        return JSONResponse({
            "ok": True,
            "path": rel,
            "display_path": _format_path(root_dir, rel),
            "name": destination.name,
            "size": bytes_written,
        })
    except FileBrowserPayloadTooLarge as exc:
        return JSONResponse({"error": str(exc)}, status_code=413)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


def file_browser_routes() -> list[Route]:
    return [
        Route("/api/files/list", endpoint=api_files_list),
        Route("/api/files/read", endpoint=api_files_read),
        Route("/api/files/content", endpoint=api_files_content),
        Route("/api/files/write", endpoint=api_files_write, methods=["POST"]),
        Route("/api/files/mkdir", endpoint=api_files_mkdir, methods=["POST"]),
        Route("/api/files/delete", endpoint=api_files_delete, methods=["POST"]),
        Route("/api/files/transfer", endpoint=api_files_transfer, methods=["POST"]),
        Route("/api/files/download", endpoint=api_files_download),
        Route("/api/files/upload", endpoint=api_files_upload, methods=["POST"]),
    ]
