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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ouroboros.config import get_ouroboroshub_catalog_url, get_ouroboroshub_skills_dir
from ouroboros.marketplace.fetcher import FetchError
from ouroboros.skill_loader import _sanitize_skill_name


_MAX_CATALOG_BYTES = 2 * 1024 * 1024
_MAX_FILE_BYTES = 5 * 1024 * 1024
_ALLOWED_HOSTS = frozenset({"raw.githubusercontent.com", "github.com", "localhost", "127.0.0.1"})


class OuroborosHubError(RuntimeError):
    pass


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        target = urllib.parse.urlparse(newurl).hostname
        if target not in _ALLOWED_HOSTS:
            raise urllib.error.URLError(
                f"OuroborosHub redirect host {target!r} is not allowed"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_AllowlistRedirectHandler())


@dataclass
class HubSkillSummary:
    slug: str
    name: str = ""
    description: str = ""
    version: str = ""
    homepage: str = ""
    files: List[Dict[str, Any]] = field(default_factory=list)
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
    if parsed.scheme not in {"https", "http"}:
        raise OuroborosHubError(f"URL must use https:// (or localhost http): {url}")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise OuroborosHubError(f"URL must use https:// for non-localhost hosts: {url}")
    if parsed.hostname not in _ALLOWED_HOSTS:
        raise OuroborosHubError(f"Host {parsed.hostname!r} is not allowed for OuroborosHub")
    with _OPENER.open(url, timeout=timeout_sec) as resp:  # noqa: S310 - host allowlist above
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise OuroborosHubError(f"Response exceeded {max_bytes} bytes: {url}")
    return data


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _catalog_url() -> str:
    return get_ouroboroshub_catalog_url()


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
    url = _catalog_url()
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
        actual = _sha256(data)
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


def _land_atomic(staging: pathlib.Path, target_dir: pathlib.Path) -> None:
    if target_dir.exists():
        sibling = target_dir.with_name(f"{target_dir.name}.replaced-ouroboroshub")
        if sibling.exists():
            shutil.rmtree(sibling, ignore_errors=True)
        target_dir.rename(sibling)
        try:
            shutil.move(str(staging), str(target_dir))
        except OSError:
            try:
                sibling.rename(target_dir)
            except OSError:
                pass
            raise
        shutil.rmtree(sibling, ignore_errors=True)
        return
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging), str(target_dir))


def install(slug: str, *, overwrite: bool = False) -> HubInstallResult:
    catalog = load_catalog()
    raw_base = str(catalog.get("raw_base_url") or "").rstrip("/")
    summary = next((item for item in _summaries(catalog) if item.slug == slug), None)
    if summary is None:
        return HubInstallResult(ok=False, sanitized_name="", error=f"skill not found: {slug}")
    sanitized = _sanitize_skill_name(summary.slug)
    target_root = get_ouroboroshub_skills_dir()
    target_dir = target_root / sanitized
    if target_dir.exists() and not overwrite:
        return HubInstallResult(ok=False, sanitized_name=sanitized, summary=summary, error=f"{sanitized} already installed")
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
            "catalog_url": _catalog_url(),
            "raw_base_url": raw_base,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "files": summary.files,
        }
        (staging / ".ouroboroshub.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _land_atomic(staging, target_dir)
        return HubInstallResult(ok=True, sanitized_name=sanitized, target_dir=target_dir, summary=summary, provenance=provenance)
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return HubInstallResult(ok=False, sanitized_name=sanitized, summary=summary, error=str(exc))


def uninstall(sanitized_name: str) -> HubInstallResult:
    name = _sanitize_skill_name(sanitized_name)
    if not name or name != sanitized_name:
        return HubInstallResult(ok=False, sanitized_name=name, error="invalid skill name")
    target = get_ouroboroshub_skills_dir() / name
    marker = target / ".ouroboroshub.json"
    if not target.exists():
        return HubInstallResult(ok=False, sanitized_name=name, error=f"{name} is not installed")
    if not marker.is_file():
        return HubInstallResult(ok=False, sanitized_name=name, error="missing OuroborosHub provenance marker")
    shutil.rmtree(target)
    return HubInstallResult(ok=True, sanitized_name=name, target_dir=target)
