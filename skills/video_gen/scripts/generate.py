"""Video generation skill — Seedance 2.0 via OpenRouter.

Submits a video generation job to the OpenRouter /api/v1/videos endpoint,
polls for completion, downloads the result, and saves it as an MP4 file
in the skill state directory (OUROBOROS_SKILL_STATE_DIR, injected by skill_exec).

Only the standard library is used (no third-party packages) so every
byte remains reviewable inside the skill pack.

The OpenRouter API key is read from the environment variable VIDEO_GEN_KEY
(injected via env_from_settings in the manifest). No secrets appear in argv.

Usage:
    generate.py <prompt...>
               [--model MODEL] [--duration SEC]
               [--aspect RATIO] [--resolution RES]
               [--out FILENAME]

``--out`` accepts a plain filename (no slashes). The file is written to
OUROBOROS_SKILL_STATE_DIR (the skill state directory injected by skill_exec).
Absolute paths and directory traversal are rejected at argument-parse time.

Exits 0 on success, 1 on error. Prints one JSON object to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


# ---------------------------------------------------------------------------
# Security constants
# ---------------------------------------------------------------------------

# Only this host is allowed for all network contact.
_ALLOWED_HOST = "openrouter.ai"
_API_BASE = f"https://{_ALLOWED_HOST}/api/v1"
_DEFAULT_MODEL = "bytedance/seedance-2.0"

# Polling / timeout
_POLL_INTERVAL_SEC = 5
_MAX_POLL_SEC = 200   # submit(30) + poll(200) + download(60) = 290 < 300 s ceiling
_DOWNLOAD_TIMEOUT_SEC = 60
_SUBMIT_TIMEOUT_SEC = 30


# ---------------------------------------------------------------------------
# Redirect-safe opener
# ---------------------------------------------------------------------------

class _StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Block cross-host redirects to prevent Authorization header leakage.

    Python's stdlib HTTPRedirectHandler does NOT strip auth headers on
    redirects, unlike `requests`. A malicious or misconfigured server could
    redirect to a different host, receiving the Bearer token verbatim.
    This handler aborts any redirect that would change the netloc.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        parsed = urllib.parse.urlparse(newurl)
        if parsed.netloc and parsed.netloc != _ALLOWED_HOST:
            raise urllib.error.URLError(
                f"Cross-host redirect blocked: {_ALLOWED_HOST!r} → {parsed.netloc!r}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_StrictRedirectHandler())


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _checked_host(url: str) -> str:
    """Raise if *url* does not point at _ALLOWED_HOST."""
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != _ALLOWED_HOST:
        raise RuntimeError(
            f"Refusing to contact non-allowlisted host: {parsed.netloc!r}"
        )
    return url


def _api_request(
    path: str,
    api_key: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    timeout: int = _SUBMIT_TIMEOUT_SEC,
) -> dict:
    """Make a JSON API request to openrouter.ai and return the parsed body.

    Uses _OPENER (with _StrictRedirectHandler) so any redirect to a
    non-openrouter.ai host raises URLError instead of silently sending
    the Bearer token to a third party.
    """
    url = _checked_host(f"{_API_BASE}{path}")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "OuroborosSkill/0.1",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download_video(url: str, out_path: str) -> None:
    """Download video bytes from *url* (must be _ALLOWED_HOST) to *out_path*.

    The OpenRouter video API returns *unsigned* download URLs — no Bearer
    token is needed. We also use _OPENER so any unexpected redirect that
    would leave openrouter.ai is blocked before any bytes are sent.
    """
    _checked_host(url)
    req = urllib.request.Request(
        url, headers={"User-Agent": "OuroborosSkill/0.1"}
    )
    with _OPENER.open(req, timeout=_DOWNLOAD_TIMEOUT_SEC) as resp:
        content = resp.read()
    with open(out_path, "wb") as fh:
        fh.write(content)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _safe_filename(name: str) -> str:
    """Validate that *name* is a plain filename with no path separators.

    Raises ValueError on any attempt to escape the target directory.
    """
    if not name:
        raise ValueError("Output filename must not be empty.")
    bad = set("/\\") | ({os.sep, os.altsep} - {None})  # type: ignore[arg-type]
    if any(ch in name for ch in bad):
        raise ValueError(
            f"Output filename {name!r} must not contain path separators."
        )
    if name in (".", "..") or name.startswith(".."):
        raise ValueError(f"Output filename {name!r} looks like a traversal attempt.")
    return name


def _default_filename() -> str:
    return f"video_{int(time.time())}.mp4"


def _output_path(filename: str) -> str:
    """Resolve the output path inside the skill state directory.

    skill_exec sets OUROBOROS_SKILL_STATE_DIR to
    ~/Ouroboros/data/state/skills/<name>/ before launching the subprocess.
    Output is always confined to that directory — never written into the
    skill source directory (which would mutate the content hash and
    invalidate the review verdict).
    """
    state_dir = os.environ.get("OUROBOROS_SKILL_STATE_DIR", "").strip()
    if not state_dir:
        raise RuntimeError(
            "OUROBOROS_SKILL_STATE_DIR is not set. "
            "This script must be run via skill_exec."
        )
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, filename)


# ---------------------------------------------------------------------------
# Business logic
# ---------------------------------------------------------------------------

def _submit_job(prompt: str, api_key: str, opts: dict) -> dict:
    """Submit a video generation job. Returns the job dict from the API."""
    body: dict = {"model": opts.get("model", _DEFAULT_MODEL), "prompt": prompt}
    if opts.get("duration"):
        body["duration"] = int(opts["duration"])
    if opts.get("aspect"):
        body["aspect_ratio"] = opts["aspect"]
    if opts.get("resolution"):
        body["resolution"] = opts["resolution"]
    return _api_request("/videos", api_key, method="POST", body=body)


def _poll_until_done(job_id: str, api_key: str) -> dict:
    """Poll /api/v1/videos/{job_id} until status is 'completed' or terminal."""
    deadline = time.monotonic() + _MAX_POLL_SEC
    while True:
        result = _api_request(f"/videos/{job_id}", api_key)
        status = result.get("status", "")
        if status == "completed":
            return result
        if status in ("failed", "cancelled"):
            raise RuntimeError(
                f"Job {job_id!r} ended with status {status!r}: "
                f"{result.get('error') or result}"
            )
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Job {job_id!r} did not complete within {_MAX_POLL_SEC} s "
                f"(last status: {status!r})"
            )
        time.sleep(_POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:  # noqa: C901
    parser = argparse.ArgumentParser(
        description="Generate a video via OpenRouter (Seedance 2.0 by default)"
    )
    parser.add_argument("prompt", nargs="+", help="Text prompt for the video")
    parser.add_argument(
        "--model", default=_DEFAULT_MODEL, help="OpenRouter model ID"
    )
    parser.add_argument("--duration", type=int, default=None, help="Duration in seconds")
    parser.add_argument("--aspect", default=None, help="Aspect ratio, e.g. 16:9")
    parser.add_argument("--resolution", default=None, help="Resolution, e.g. 720p")
    parser.add_argument(
        "--out", default=None,
        help="Output filename (plain name, no slashes). Written to system temp dir."
    )
    args = parser.parse_args(argv)

    prompt = " ".join(args.prompt).strip()
    if not prompt:
        print(json.dumps({"error": "prompt is empty"}))
        return 2

    # Read API key from environment (injected via env_from_settings: [VIDEO_GEN_KEY]).
    api_key = os.environ.get("VIDEO_GEN_KEY", "").strip()
    if not api_key:
        print(json.dumps({
            "error": (
                "VIDEO_GEN_KEY is not set. "
                "Add it in Ouroboros Settings with your OpenRouter API key value."
            )
        }))
        return 1

    # Validate filename and resolve to temp dir (outside skill dir).
    try:
        filename = _safe_filename(args.out) if args.out else _default_filename()
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    out_path = _output_path(filename)

    opts = {
        "model": args.model,
        "duration": args.duration,
        "aspect": args.aspect,
        "resolution": args.resolution,
    }

    job_id: str | None = None
    try:
        # 1. Submit
        job = _submit_job(prompt, api_key, opts)
        job_id = job.get("id") or job.get("generation_id", "unknown")

        # 2. Poll
        result = _poll_until_done(job_id, api_key)

        # 3. Download — try known field names for video URLs:
        # https://openrouter.ai/docs/api/api-reference/video-generation/get-videos
        urls = (
            result.get("unsigned_urls")
            or result.get("output_urls")
            or [v.get("url") for v in result.get("videos") or [] if v.get("url")]
        )
        if not urls:
            raise RuntimeError(f"No download URLs in completed job: {result}")
        download_url = _checked_host(urls[0])
        _download_video(download_url, out_path)

        cost = (result.get("usage") or {}).get("cost", None)
        output: dict = {
            "status": "completed",
            "output_path": os.path.abspath(out_path),
            "job_id": job_id,
        }
        if cost is not None:
            output["cost_usd"] = cost
        print(json.dumps(output, ensure_ascii=False))
        return 0

    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        print(json.dumps({"error": f"HTTP {exc.code}: {body}", "job_id": job_id}))
        return 1
    except urllib.error.URLError as exc:
        print(json.dumps({"error": f"network: {exc.reason!r}", "job_id": job_id}))
        return 1
    except TimeoutError as exc:
        print(json.dumps({"error": str(exc), "job_id": job_id}))
        return 1
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}", "job_id": job_id}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
