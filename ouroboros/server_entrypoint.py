"""CLI and port-binding helpers extracted from server.py."""

from __future__ import annotations

import argparse
import pathlib
import socket


def _can_bind_port(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(host: str, start: int = 8765, max_tries: int = 10,
                   wait_retries: int = 20, wait_interval: float = 0.5) -> int:
    """Try the preferred port first; wait for it to become free on restart.

    During a restart the previous process may still be releasing the socket
    (TIME_WAIT / lingering close).  Instead of silently moving to the next
    port (which breaks WebSocket reconnect because the browser still points
    at the old port), we retry the *preferred* port with short sleeps first.

    Only if the preferred port is still occupied after ``wait_retries`` attempts
    do we fall back to scanning nearby ports.
    """
    import time

    for attempt in range(wait_retries):
        if _can_bind_port(host, start):
            return start
        if attempt < wait_retries - 1:
            time.sleep(wait_interval)

    # Preferred port still busy — nearby ports may still be winding down too.
    # Retry the fallback range instead of returning the original busy port.
    fallback_ports = range(start + 1, start + max_tries)
    for attempt in range(wait_retries):
        for port in fallback_ports:
            if _can_bind_port(host, port):
                return port
        if attempt < wait_retries - 1:
            time.sleep(wait_interval)

    raise OSError(f"No free port available in range {start}-{start + max_tries - 1}")


def parse_server_args(default_host: str, default_port: int) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Ouroboros web server.")
    parser.add_argument(
        "--host",
        default=default_host,
        help="Host interface to bind (default: %(default)s or OUROBOROS_SERVER_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help="Port to bind (default: %(default)s or OUROBOROS_SERVER_PORT).",
    )
    return parser.parse_args()


def write_port_file(port_file: pathlib.Path, port: int) -> None:
    port_file.parent.mkdir(parents=True, exist_ok=True)
    port_file.write_text(str(port), encoding="utf-8")
