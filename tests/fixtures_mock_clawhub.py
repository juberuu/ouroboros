from __future__ import annotations

import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _skill_archive() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "duck/SKILL.md",
            "---\n"
            "name: duck\n"
            "description: mock search skill\n"
            "version: 1.0.0\n"
            "metadata:\n"
            "  openclaw:\n"
            "    install:\n"
            "      - kind: pip\n"
            "        package: ddgs\n"
            "---\n",
        )
    return buf.getvalue()


class MockClawHubServer:
    def __init__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib callback name
        if self.path.startswith("/packages/duck") or self.path.startswith("/skills/duck"):
            payload = {
                "slug": "duck",
                "name": "duck",
                "version": "1.0.0",
                "metadata": {"openclaw": {"install": [{"kind": "pip", "package": "ddgs"}]}},
            }
            return self._json(payload)
        if self.path.startswith("/download/duck"):
            data = _skill_archive()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json({"packages": []})

    def _json(self, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        return
