"""Read-only HTTP client for the ClawHub registry (https://clawhub.ai/api/v1).

The client wraps four operations the marketplace UI / installer need:

- :func:`search` — keyword + filter search of the skill registry.
- :func:`info` — resolve the latest version metadata for one skill.
- :func:`list_versions` — every published version for one slug.
- :func:`download` — fetch the binary archive bytes for one version.

Defensive design choices that matter for security:

- Hostname allowlist: only ``clawhub.ai`` (or the configured registry
  URL's host) is contacted. Arbitrary user-supplied URLs cannot
  redirect requests to a different host even if the registry returns
  a malformed response.
- Aggressive timeouts (default 15s) so the UI never blocks on a
  hung registry call.
- No authentication. The marketplace surface only exposes anonymous
  read operations — publish/sync workflows live in the upstream
  ``clawhub`` CLI which Ouroboros never invokes.
- Response size cap: the JSON endpoints reject anything > 4 MB and
  the download endpoint applies the 50 MB cap from
  :mod:`ouroboros.marketplace.fetcher`. This prevents a malicious
  registry from filling the runtime with a multi-GB response.

The client is intentionally synchronous (``requests`` /
``urllib`` style). Marketplace HTTP routes call it from
``asyncio.to_thread`` so the Starlette event loop stays responsive.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_SEC = 15
_MAX_JSON_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MB JSON cap
_MAX_ARCHIVE_BYTES = 50 * 1024 * 1024       # mirrors fetcher cap
_USER_AGENT = "Ouroboros-Marketplace/4.50 (+https://github.com/joi-lab/ouroboros-desktop)"

# Allowed registry hosts. The canonical registry is ``clawhub.ai`` (per
# the official docs at github.com/openclaw/clawhub). ``clawhub.com``
# is documented as a legacy alias but Ouroboros does not enforce that
# it shares ownership with ``clawhub.ai`` — until we have an
# independent ownership audit (DNS / TLS pin / signed metadata), we
# refuse it. Localhost remains allowlisted for self-hosted dev mirrors.
_ALLOWED_REGISTRY_HOSTS = frozenset(
    {
        "clawhub.ai",
        "www.clawhub.ai",
        "registry.clawhub.ai",
        "localhost",
        "127.0.0.1",
    }
)


class ClawHubClientError(RuntimeError):
    """Raised on any registry-side failure (HTTP, JSON, host policy)."""


class ClawHubClientHostBlocked(ClawHubClientError):
    """Registry URL points at a host outside the marketplace allowlist."""


@dataclass
class ClawHubSkillSummary:
    """Lightweight per-skill record returned by ``search`` / ``info``.

    Mirrors the schema described in
    https://github.com/openclaw/clawhub/blob/main/docs/spec.md but is
    intentionally permissive: any field that the registry omits is
    rendered as the empty string / 0 / [] so downstream UI code can
    render the card without conditional branches.
    """

    slug: str
    display_name: str = ""
    summary: str = ""
    description: str = ""
    latest_version: str = ""
    versions: List[str] = field(default_factory=list)
    license: str = ""
    homepage: str = ""
    os_list: List[str] = field(default_factory=list)
    requires_env: List[str] = field(default_factory=list)
    requires_bins: List[str] = field(default_factory=list)
    primary_env: str = ""
    install_specs: List[Dict[str, Any]] = field(default_factory=list)
    badges: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)
    is_plugin: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "summary": self.summary,
            "description": self.description,
            "latest_version": self.latest_version,
            "versions": list(self.versions),
            "license": self.license,
            "homepage": self.homepage,
            "os": list(self.os_list),
            "requires_env": list(self.requires_env),
            "requires_bins": list(self.requires_bins),
            "primary_env": self.primary_env,
            "install_specs": list(self.install_specs),
            "badges": dict(self.badges),
            "stats": dict(self.stats),
            "is_plugin": self.is_plugin,
        }


def _registry_base_url(override: Optional[str] = None) -> str:
    """Return the resolved registry base URL with trailing slash stripped.

    Defers to :func:`ouroboros.config.get_clawhub_registry_url` when no
    explicit override is supplied. Hostnames outside the marketplace
    allowlist raise :class:`ClawHubClientHostBlocked` so a malicious
    settings override cannot redirect HTTP traffic.
    """
    if override is None or not str(override).strip():
        from ouroboros.config import get_clawhub_registry_url
        url = get_clawhub_registry_url()
    else:
        url = str(override).strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ClawHubClientHostBlocked(
            f"Registry URL {url!r} must use https:// (or http:// for localhost dev)."
        )
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ClawHubClientHostBlocked(
            f"Registry URL {url!r} must use https:// for non-localhost hosts."
        )
    if not parsed.hostname:
        raise ClawHubClientHostBlocked(f"Registry URL {url!r} has no hostname.")
    if parsed.hostname not in _ALLOWED_REGISTRY_HOSTS:
        raise ClawHubClientHostBlocked(
            f"Registry host {parsed.hostname!r} is not in the marketplace "
            f"allowlist {sorted(_ALLOWED_REGISTRY_HOSTS)}."
        )
    return url.rstrip("/")


def _build_url(base: str, path: str, query: Optional[Dict[str, Any]] = None) -> str:
    """Join ``base`` + ``path`` defensively (preserves any base path).

    See https://github.com/openclaw/clawhub/issues/433 — ``new URL(path,
    base)`` semantics drop the base path when ``path`` starts with ``/``.
    We strip leading slashes from path and append manually so a
    subpath-deployed registry still works.
    """
    rel = path.lstrip("/")
    composed = f"{base.rstrip('/')}/{rel}"
    if query:
        composed = f"{composed}?{urllib.parse.urlencode(query, doseq=True)}"
    return composed


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse 30x redirects whose target host leaves the marketplace allowlist.

    The default ``urllib.request.urlopen`` opener follows redirects
    transparently with no host re-validation, which would otherwise
    let the registry hand us a ``302 Location: https://evil.example.com/...``
    and exfiltrate / SSRF-pivot the request. Substituting this handler
    closes that loophole — every hop must independently satisfy
    :data:`_ALLOWED_REGISTRY_HOSTS`.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        target = urllib.parse.urlparse(newurl).hostname
        if target not in _ALLOWED_REGISTRY_HOSTS:
            raise ClawHubClientHostBlocked(
                f"Refused to follow redirect to {target!r} outside marketplace allowlist"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_AllowlistRedirectHandler())


def _http_get(
    url: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT_SEC,
    accept: str = "application/json",
    max_bytes: int = _MAX_JSON_RESPONSE_BYTES,
) -> Tuple[bytes, Dict[str, str]]:
    """Issue a GET, capping the response to ``max_bytes`` bytes.

    Reads the response body in 64 KiB chunks and aborts as soon as the
    cap is exceeded. Returns ``(body_bytes, headers)``. Raises
    :class:`ClawHubClientError` on transport / HTTP / oversize errors,
    or :class:`ClawHubClientHostBlocked` on a redirect target outside
    :data:`_ALLOWED_REGISTRY_HOSTS`.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": accept,
        },
        method="GET",
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or response.getcode() or 0)
            if status >= 400:
                raise ClawHubClientError(
                    f"GET {url} returned HTTP {status}"
                )
            buf = bytearray()
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                if len(buf) + len(chunk) > max_bytes:
                    raise ClawHubClientError(
                        f"GET {url} response exceeds {max_bytes} byte cap "
                        "(possible registry abuse)."
                    )
                buf.extend(chunk)
            headers = {k.lower(): v for k, v in response.headers.items()}
            return bytes(buf), headers
    except urllib.error.HTTPError as exc:
        raise ClawHubClientError(f"GET {url}: HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ClawHubClientError(f"GET {url}: transport error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ClawHubClientError(f"GET {url}: timed out after {timeout}s") from exc


def _decode_json(body: bytes, *, url: str) -> Any:
    try:
        text = body.decode("utf-8", errors="replace")
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClawHubClientError(
            f"GET {url} returned invalid JSON: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Summary parsing
# ---------------------------------------------------------------------------


def _coerce_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _detect_plugin(raw: Dict[str, Any]) -> bool:
    """Return True when the registry record looks like a Node plugin.

    OpenClaw plugins announce themselves via either an explicit
    ``kind: plugin`` discriminator (newer registry shape) or by
    bundling an ``openclaw.plugin.json`` in the package — the registry
    indexes that under ``plugin_manifest`` / ``has_plugin`` /
    ``package_kind == 'plugin'``. We accept any of these as a plugin
    flag; the marketplace UI hides plugins from search results.
    """
    kind = str(raw.get("kind") or raw.get("package_kind") or "").lower()
    if kind == "plugin":
        return True
    if raw.get("plugin_manifest"):
        return True
    if raw.get("has_plugin") is True:
        return True
    return False


def _extract_metadata_openclaw(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the ``metadata.openclaw`` block out of a parsed record.

    The registry sometimes nests metadata one level deep
    (``metadata.openclaw``), sometimes uses the legacy ``clawdis`` /
    ``clawdbot`` aliases, and sometimes flattens fields onto the
    top-level record. We consult all three and the FIRST non-empty
    block wins.
    """
    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    for key in ("openclaw", "clawdis", "clawdbot"):
        block = metadata.get(key)
        if isinstance(block, dict) and block:
            return block
    parsed = raw.get("parsed")
    if isinstance(parsed, dict):
        nested = parsed.get("metadata", {})
        if isinstance(nested, dict):
            for key in ("openclaw", "clawdis", "clawdbot"):
                block = nested.get(key)
                if isinstance(block, dict) and block:
                    return block
    return {}


def _summary_from_record(raw: Any) -> ClawHubSkillSummary:
    """Build a :class:`ClawHubSkillSummary` from one registry record."""
    if not isinstance(raw, dict):
        raise ClawHubClientError(
            f"Registry record must be an object, got {type(raw).__name__}"
        )
    slug = (
        str(raw.get("slug") or raw.get("name") or "").strip()
    )
    if not slug:
        raise ClawHubClientError("Registry record missing required 'slug'/'name'")

    display_name = str(raw.get("displayName") or raw.get("display_name") or slug).strip()
    summary_text = str(raw.get("summary") or raw.get("description") or "").strip()
    description = str(raw.get("description") or summary_text).strip()

    latest_version = str(raw.get("latestVersion") or raw.get("latest_version") or raw.get("version") or "").strip()
    versions = _coerce_str_list(raw.get("versions") or raw.get("availableVersions"))
    if latest_version and latest_version not in versions:
        versions.insert(0, latest_version)

    license_text = str(raw.get("license") or "").strip()
    homepage = str(raw.get("homepage") or raw.get("website") or raw.get("url") or "").strip()

    metadata_block = _extract_metadata_openclaw(raw)
    requires = metadata_block.get("requires") or {}
    if not isinstance(requires, dict):
        requires = {}
    os_list = _coerce_str_list(metadata_block.get("os") or raw.get("os"))
    requires_env = _coerce_str_list(requires.get("env"))
    requires_bins = _coerce_str_list(requires.get("bins") or requires.get("anyBins"))
    primary_env = str(metadata_block.get("primaryEnv") or "").strip()
    install_specs_raw = metadata_block.get("install") or []
    install_specs: List[Dict[str, Any]] = []
    if isinstance(install_specs_raw, list):
        for spec in install_specs_raw:
            if isinstance(spec, dict):
                install_specs.append(dict(spec))

    badges = raw.get("badges") or {}
    if not isinstance(badges, dict):
        badges = {}
    stats = raw.get("stats") or {}
    if not isinstance(stats, dict):
        stats = {}

    return ClawHubSkillSummary(
        slug=slug,
        display_name=display_name,
        summary=summary_text,
        description=description,
        latest_version=latest_version,
        versions=versions,
        license=license_text,
        homepage=homepage,
        os_list=os_list,
        requires_env=requires_env,
        requires_bins=requires_bins,
        primary_env=primary_env,
        install_specs=install_specs,
        badges=badges,
        stats=stats,
        is_plugin=_detect_plugin(raw),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Public client surface
# ---------------------------------------------------------------------------


def search(
    query: str = "",
    *,
    limit: int = 25,
    offset: int = 0,
    sort: str = "downloads",
    registry_url: Optional[str] = None,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
) -> List[ClawHubSkillSummary]:
    """Search the registry for skills matching ``query``.

    The registry's search endpoint is documented as
    ``GET /skills?limit=25`` (per
    https://github.com/openclaw/clawhub/blob/main/docs/spec.md). When
    additional filters are exposed by the registry they are forwarded
    via ``q``, ``sort``, ``offset`` query parameters.
    """
    base = _registry_base_url(registry_url)
    safe_limit = max(1, min(int(limit or 25), 100))
    safe_offset = max(0, int(offset or 0))
    sort_key = (sort or "downloads").strip().lower()
    if sort_key not in {"downloads", "stars", "recent", "name"}:
        sort_key = "downloads"
    query_params: Dict[str, Any] = {
        "limit": safe_limit,
        "offset": safe_offset,
        "sort": sort_key,
    }
    cleaned_query = (query or "").strip()
    if cleaned_query:
        query_params["q"] = cleaned_query
    url = _build_url(base, "skills", query_params)
    body, _headers = _http_get(url, timeout=timeout_sec)
    parsed = _decode_json(body, url=url)

    # Registry may return either {"skills": [...]} or a bare array.
    if isinstance(parsed, dict):
        items = parsed.get("skills") or parsed.get("results") or parsed.get("items") or []
    elif isinstance(parsed, list):
        items = parsed
    else:
        raise ClawHubClientError(
            f"Unexpected response shape from {url}: {type(parsed).__name__}"
        )

    summaries: List[ClawHubSkillSummary] = []
    for record in items:
        try:
            summaries.append(_summary_from_record(record))
        except ClawHubClientError:
            log.warning("Skipping malformed registry record: %r", record, exc_info=True)
            continue
    return summaries


def _validate_slug(slug: str) -> str:
    """Strip + validate a slug before sending it to the registry.

    Slugs reach this function via the HTTP route ``{slug:path}`` which
    accepts arbitrary path segments including ``..`` traversal. The
    install pipeline's ``sanitize_clawhub_slug`` already protects the
    on-disk landing path, but a slug like ``../../etc`` would still be
    issued as ``GET /api/v1/skills/../../etc`` to the registry — a
    small SSRF amplifier letting an external HTTP caller probe
    arbitrary registry paths via the Ouroboros server. We reject ``..``
    segments and absolute paths up-front so the registry only sees
    well-formed slug shapes.
    """
    cleaned = (slug or "").strip()
    if not cleaned:
        raise ClawHubClientError("'slug' must be non-empty")
    if cleaned.startswith("/") or cleaned.startswith("\\"):
        raise ClawHubClientError("'slug' must not be absolute")
    parts = cleaned.replace("\\", "/").split("/")
    if any(part == ".." or part == "." for part in parts):
        raise ClawHubClientError("'slug' must not contain '..' or '.' segments")
    return cleaned


def info(
    slug: str,
    *,
    registry_url: Optional[str] = None,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
) -> ClawHubSkillSummary:
    """Resolve the latest version metadata for ``slug``."""
    cleaned = _validate_slug(slug)
    base = _registry_base_url(registry_url)
    url = _build_url(base, f"skills/{urllib.parse.quote(cleaned, safe='/-')}")
    body, _headers = _http_get(url, timeout=timeout_sec)
    parsed = _decode_json(body, url=url)
    if isinstance(parsed, dict) and "skill" in parsed:
        return _summary_from_record(parsed["skill"])
    return _summary_from_record(parsed)


def list_versions(
    slug: str,
    *,
    registry_url: Optional[str] = None,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
) -> List[str]:
    """Return every published version for ``slug`` (latest first when known)."""
    summary = info(slug, registry_url=registry_url, timeout_sec=timeout_sec)
    versions = list(summary.versions)
    if summary.latest_version and summary.latest_version not in versions:
        versions.insert(0, summary.latest_version)
    return versions


@dataclass
class ClawHubArchive:
    """Result of a successful :func:`download` call."""
    slug: str
    version: str
    content: bytes
    sha256: str
    content_type: str = ""

    def __post_init__(self) -> None:
        if not self.content:
            raise ClawHubClientError("Downloaded archive is empty")


def download(
    slug: str,
    *,
    version: Optional[str] = None,
    registry_url: Optional[str] = None,
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC * 2,
) -> ClawHubArchive:
    """Download the binary archive for ``slug`` (specific version or latest).

    The download endpoint is documented as ``GET /download/{skill}``
    (with optional ``?version=`` query). On success returns the bytes
    + a sha256 fingerprint computed locally over those bytes.

    Important: the returned ``sha256`` is the fingerprint of WHAT WE
    RECEIVED, not a registry-side advertised digest. The current
    ClawHub registry does not expose a separate per-version digest in
    its ``info`` response, so there is no out-of-band integrity anchor
    we can cross-check against. The TLS connection to ``clawhub.ai``
    is the only integrity gate the marketplace enjoys today; the
    fingerprint here is useful for provenance + change-detection on
    update flows, NOT as an MITM defense.
    """
    import hashlib
    cleaned = _validate_slug(slug)
    base = _registry_base_url(registry_url)
    query: Dict[str, Any] = {}
    cleaned_version = (version or "").strip()
    if cleaned_version:
        query["version"] = cleaned_version
    url = _build_url(
        base,
        f"download/{urllib.parse.quote(cleaned, safe='/-')}",
        query or None,
    )
    body, headers = _http_get(
        url,
        timeout=timeout_sec,
        accept="application/octet-stream, application/zip",
        max_bytes=_MAX_ARCHIVE_BYTES,
    )
    if len(body) == 0:
        raise ClawHubClientError(f"Empty archive returned by {url}")
    return ClawHubArchive(
        slug=cleaned,
        version=cleaned_version,
        content=body,
        sha256=hashlib.sha256(body).hexdigest(),
        content_type=headers.get("content-type", ""),
    )


__all__ = [
    "ClawHubArchive",
    "ClawHubClientError",
    "ClawHubClientHostBlocked",
    "ClawHubSkillSummary",
    "download",
    "info",
    "list_versions",
    "search",
]
