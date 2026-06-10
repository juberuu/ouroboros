"""Static GitHub catalog client for official OuroborosHub skills."""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from ouroboros.marketplace import AllowlistRedirectHandler
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ouroboros.config import get_ouroboroshub_catalog_url, get_ouroboroshub_skills_dir
from ouroboros.marketplace.fetcher import FetchError, land_staged_tree
from ouroboros.marketplace.install_specs import install_specs_hash
from ouroboros.marketplace.isolated_deps import DEPS_STATE_FILENAME, read_deps_state
from ouroboros.skill_dependencies import normalize_declared_dependency_specs
from ouroboros.skill_loader import _sanitize_skill_name, skill_state_dir
from ouroboros.utils import atomic_write_json, utc_now_iso


_MAX_CATALOG_BYTES = 2 * 1024 * 1024
_MAX_FILE_BYTES = 5 * 1024 * 1024
_ALLOWED_HOSTS = frozenset({"raw.githubusercontent.com", "github.com", "localhost", "127.0.0.1"})


class OuroborosHubError(RuntimeError):
    pass


def _raise_if(condition: bool, message: str) -> None:
    if condition:
        raise OuroborosHubError(message)


# Lazy, proxy-free opener: an import-time build_opener snapshots the process
# proxy environment (and triggers macOS proxy lookup in forked workers); the
# clawhub module's lazy no-proxy pattern is the SSOT behavior to match.
_OPENER: urllib.request.OpenerDirector | None = None


def _hub_opener() -> urllib.request.OpenerDirector:
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            AllowlistRedirectHandler(
                _ALLOWED_HOSTS,
                lambda target: urllib.error.URLError(
                    f"OuroborosHub redirect host {target!r} is not allowed"
                ),
            ),
        )
    return _OPENER


@dataclass
class HubSkillSummary:
    slug: str
    name: str = ""
    description: str = ""
    version: str = ""
    homepage: str = ""
    files: List[Dict[str, Any]] = field(default_factory=list)
    install_specs: Any = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slug": self.slug,
            "display_name": self.name or self.slug,
            "summary": self.description,
            "description": self.description,
            "latest_version": self.version,
            "versions": [self.version] if self.version else [],
            "homepage": self.homepage,
            "install_specs": self.install_specs,
            "source": "ouroboroshub",
            "stats": {},
            "badges": {"official": True},
            "is_plugin": False,
        }


@dataclass
class HubInstallResult:
    ok: bool
    sanitized_name: str
    error: str = ""
    target_dir: Optional[pathlib.Path] = None
    summary: Optional[HubSkillSummary] = None
    provenance: Dict[str, Any] = field(default_factory=dict)


def _fetch_bytes(url: str, *, max_bytes: int, timeout_sec: int = 15) -> bytes:
    parsed = urllib.parse.urlparse(url)
    _raise_if(parsed.scheme not in {"https", "http"}, f"URL must use https:// (or localhost http): {url}")
    _raise_if(parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}, f"URL must use https:// for non-localhost hosts: {url}")
    _raise_if(parsed.hostname not in _ALLOWED_HOSTS, f"Host {parsed.hostname!r} is not allowed for OuroborosHub")
    with _hub_opener().open(url, timeout=timeout_sec) as resp:  # noqa: S310 - host allowlist above
        data = resp.read(max_bytes + 1)
    _raise_if(len(data) > max_bytes, f"Response exceeded {max_bytes} bytes: {url}")
    return data


def _raw_base(catalog: Dict[str, Any], catalog_url: str) -> str:
    raw_base = str(catalog.get("raw_base_url") or "").rstrip("/")
    if raw_base:
        return raw_base
    parsed = urllib.parse.urlparse(catalog_url)
    if parsed.hostname == "raw.githubusercontent.com":
        path = parsed.path.strip("/").split("/")
        if len(path) >= 3:
            owner, repo, ref = path[:3]
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}"
    raise OuroborosHubError("catalog must include raw_base_url")


def load_catalog() -> Dict[str, Any]:
    url = get_ouroboroshub_catalog_url()
    data = _fetch_bytes(url, max_bytes=_MAX_CATALOG_BYTES)
    try:
        catalog = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OuroborosHubError(f"catalog is not valid JSON: {exc}") from exc
    if not isinstance(catalog, dict):
        raise OuroborosHubError("catalog root must be an object")
    catalog.setdefault("raw_base_url", _raw_base(catalog, url))
    return catalog


def _summaries(catalog: Dict[str, Any]) -> List[HubSkillSummary]:
    raw_skills = catalog.get("skills") or []
    if not isinstance(raw_skills, list):
        raise OuroborosHubError("catalog.skills must be a list")
    out: List[HubSkillSummary] = []
    for item in raw_skills:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").strip()
        if not slug:
            continue
        out.append(
            HubSkillSummary(
                slug=slug,
                name=str(item.get("name") or slug),
                description=str(item.get("description") or ""),
                version=str(item.get("version") or ""),
                homepage=str(item.get("homepage") or ""),
                files=list(item.get("files") or []),
                install_specs=item.get("install_specs") or item.get("install") or [],
                raw=item,
            )
        )
    return out


def search(query: str = "") -> List[HubSkillSummary]:
    q = str(query or "").strip().lower()
    entries = _summaries(load_catalog())
    if not q:
        return entries
    return [
        item for item in entries
        if q in item.slug.lower() or q in item.name.lower() or q in item.description.lower()
    ]


def info(slug: str) -> HubSkillSummary:
    for item in _summaries(load_catalog()):
        if item.slug == slug:
            return item
    raise OuroborosHubError(f"OuroborosHub skill not found: {slug}")


def _safe_rel(path: str) -> pathlib.PurePosixPath:
    text = str(path or "").strip()
    if "\\" in text or ":" in text:
        raise FetchError(f"unsafe catalog file path: {path!r}")
    rel = pathlib.PurePosixPath(text)
    if not rel.parts or rel.is_absolute() or ".." in rel.parts:
        raise FetchError(f"unsafe catalog file path: {path!r}")
    if any(part in {"node_modules", ".ouroboros_env"} for part in rel.parts):
        raise FetchError(f"catalog file path uses review-opaque dependency directory: {path!r}")
    if "__pycache__" in rel.parts or rel.suffix.lower() in {".pyc", ".pyo", ".so", ".dylib", ".dll", ".wasm"}:
        raise FetchError(f"catalog file path uses generated or binary artifact: {path!r}")
    return rel


def _download_skill_files(summary: HubSkillSummary, raw_base: str, staging_dir: pathlib.Path) -> None:
    files = summary.files
    if not files:
        raise OuroborosHubError(f"catalog entry {summary.slug!r} has no files")
    for item in files:
        if not isinstance(item, dict):
            raise OuroborosHubError(f"catalog file entry for {summary.slug!r} is not an object")
        rel = _safe_rel(str(item.get("path") or ""))
        expected = str(item.get("sha256") or "").strip().lower()
        if not expected:
            raise OuroborosHubError(f"catalog file {rel} is missing sha256")
        url = f"{raw_base.rstrip('/')}/skills/{urllib.parse.quote(summary.slug)}/{urllib.parse.quote(rel.as_posix(), safe='/')}"
        data = _fetch_bytes(url, max_bytes=_MAX_FILE_BYTES)
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise OuroborosHubError(f"sha256 mismatch for {rel}: expected {expected}, got {actual}")
        target = staging_dir / pathlib.Path(*rel.parts)
        try:
            target.resolve(strict=False).relative_to(staging_dir.resolve(strict=False))
        except ValueError as exc:
            raise FetchError(f"catalog file path escapes staging dir: {rel}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if not (staging_dir / "SKILL.md").is_file():
        raise OuroborosHubError(f"catalog entry {summary.slug!r} did not include SKILL.md")


def _read_hub_marker(target_dir: pathlib.Path) -> Dict[str, Any]:
    marker = pathlib.Path(target_dir) / ".ouroboroshub.json"
    if not marker.is_file():
        return {}
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _valid_existing_hub_marker(target_dir: pathlib.Path, sanitized: str) -> Dict[str, Any]:
    marker = _read_hub_marker(target_dir)
    marker_slug = str(marker.get("slug") or "").strip()
    try:
        schema_version = int(marker.get("schema_version") or 0)
    except (TypeError, ValueError):
        schema_version = 0
    if (
        schema_version == 1
        and str(marker.get("source") or "") == "ouroboroshub"
        and str(marker.get("sanitized_name") or "") == sanitized
        and marker_slug
        and _sanitize_skill_name(marker_slug) == sanitized
    ):
        return marker
    return {}


def _has_repairable_hub_partial(drive_root: pathlib.Path, sanitized: str, target_dir: pathlib.Path) -> bool:
    target = pathlib.Path(target_dir)
    return (
        (skill_state_dir(drive_root, sanitized) / DEPS_STATE_FILENAME).is_file()
        or (target / ".ouroboros_env").exists()
        or (target / ".ouroboroshub.json").is_file()
    )


def install(slug: str, *, overwrite: bool = False) -> HubInstallResult:
    catalog = load_catalog()
    raw_base = str(catalog.get("raw_base_url") or "").rstrip("/")
    summary = next((item for item in _summaries(catalog) if item.slug == slug), None)
    if summary is None:
        return HubInstallResult(False, "", error=f"skill not found: {slug}")
    sanitized = _sanitize_skill_name(summary.slug)
    target_root = get_ouroboroshub_skills_dir()
    target_dir = target_root / sanitized
    raw_install = summary.install_specs or summary.raw.get("dependencies") or []
    auto_specs, manual_specs, _warnings = normalize_declared_dependency_specs(raw_install)
    if target_dir.exists() and not overwrite:
        deps_state = read_deps_state(target_root.parent.parent, sanitized, target_dir)
        marker = _valid_existing_hub_marker(target_dir, sanitized)
        if (
            auto_specs
            and str(deps_state.get("status") or "") == "installed"
            and str(deps_state.get("specs_hash") or "") == install_specs_hash(auto_specs)
            and marker
        ):
            atomic_write_json(
                skill_state_dir(target_root.parent.parent, sanitized) / DEPS_STATE_FILENAME,
                deps_state,
                trailing_newline=True,
            )
            return HubInstallResult(True, sanitized, target_dir=target_dir, summary=summary, provenance=marker)
        if not _has_repairable_hub_partial(target_root.parent.parent, sanitized, target_dir):
            return HubInstallResult(False, sanitized, error=f"{sanitized} already installed", summary=summary)
    staging_root = target_root / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(tempfile.mkdtemp(prefix="ouroboroshub_skill_", dir=str(staging_root)))
    try:
        _download_skill_files(summary, raw_base, staging)
        provenance = {
            "schema_version": 1,
            "source": "ouroboroshub",
            "slug": summary.slug,
            "sanitized_name": sanitized,
            "version": summary.version,
            "catalog_url": get_ouroboroshub_catalog_url(),
            "raw_base_url": raw_base,
            "installed_at": utc_now_iso(),
            "files": summary.files,
        }
        if auto_specs or manual_specs:
            provenance["install_specs"] = {
                "schema_version": 1,
                "auto": auto_specs,
                "manual": manual_specs,
                "raw": raw_install,
                "specs_hash": install_specs_hash(auto_specs),
            }
        atomic_write_json(staging / ".ouroboroshub.json", provenance, trailing_newline=True)
        land_staged_tree(staging, target_dir, replacement_suffix="replaced-ouroboroshub")
        return HubInstallResult(True, sanitized, target_dir=target_dir, summary=summary, provenance=provenance)
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return HubInstallResult(False, sanitized, error=str(exc), summary=summary)


def uninstall(sanitized_name: str) -> HubInstallResult:
    name = _sanitize_skill_name(sanitized_name)
    if not name or name != sanitized_name:
        return HubInstallResult(False, name, error="invalid skill name")
    target_root = get_ouroboroshub_skills_dir()
    target = target_root / name
    marker = target / ".ouroboroshub.json"
    if not target.exists():
        return HubInstallResult(False, name, error=f"{name} is not installed")
    if not marker.is_file():
        return HubInstallResult(False, name, error="missing OuroborosHub provenance marker")
    # Unload live extension before removing payload so registries do not point at deleted modules.
    try:
        from ouroboros.extension_loader import unload_extension
        unload_extension(name)
    except Exception:  # pragma: no cover — defensive
        pass
    shutil.rmtree(target)
    try:
        (skill_state_dir(target_root.parent.parent, name) / DEPS_STATE_FILENAME).unlink(missing_ok=True)
    except Exception:
        pass
    return HubInstallResult(True, name, target_dir=target)
