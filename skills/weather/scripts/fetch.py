"""Weather reference skill — Phase 5 PoC.

Fetches a compact summary from the public ``wttr.in`` service. No API
key, no stored credentials, no persistent state. Prints one JSON
object to stdout.

The script intentionally uses the standard library only: installing a
third-party HTTP client from inside a skill subprocess would break the
"every reviewed byte" promise ``review_skill`` makes about its pack.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request


# Refuse to reach anywhere except this exact host. Matches the
# declared ``permissions: [net]`` minimum — reviewers should flag
# any attempt to widen the allowlist.
_ALLOWED_HOST = "wttr.in"
_TIMEOUT_SEC = 10


def _fetch(city: str) -> dict:
    url = f"https://{_ALLOWED_HOST}/{urllib.parse.quote(city)}?format=j1"
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != _ALLOWED_HOST:
        raise RuntimeError(f"Refusing to fetch from non-allowlisted host: {parsed.netloc}")
    req = urllib.request.Request(url, headers={"User-Agent": "OuroborosSkill/0.1"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    current = (data.get("current_condition") or [{}])[0]
    return {
        "city": city,
        "temp_c": int(current.get("temp_C") or 0),
        "condition": (current.get("weatherDesc") or [{}])[0].get("value") or "Unknown",
        "feels_like_c": int(current.get("FeelsLikeC") or 0),
        "humidity_pct": int(current.get("humidity") or 0),
    }


def main(argv: list[str]) -> int:
    if len(argv) < 1:
        print(json.dumps({"error": "usage: fetch.py <city>"}))
        return 2
    city = " ".join(argv).strip()
    if not city:
        print(json.dumps({"error": "city argument is empty"}))
        return 2
    try:
        summary = _fetch(city)
    except urllib.error.URLError as exc:
        print(json.dumps({"error": f"network: {exc.reason!r}"}))
        return 1
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
