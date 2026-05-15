"""Thin command-line entrypoint for source and packaged runs."""

from __future__ import annotations

import argparse
import os
import sys


def _server_command(args: argparse.Namespace) -> int:
    if args.host:
        os.environ["OUROBOROS_SERVER_HOST"] = args.host
    if args.port:
        os.environ["OUROBOROS_SERVER_PORT"] = str(args.port)
    import server

    old_argv = sys.argv[:]
    try:
        sys.argv = [old_argv[0]]
        return int(server.main())
    finally:
        sys.argv = old_argv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ouroboros")
    subparsers = parser.add_subparsers(dest="command")

    server_parser = subparsers.add_parser("server", help="run the Ouroboros web server")
    server_parser.add_argument("--host", default="", help="host/interface to bind")
    server_parser.add_argument("--port", type=int, default=0, help="port to bind")
    server_parser.add_argument("--no-ui", action="store_true", help="accepted for CLI parity; server mode has no desktop UI")
    server_parser.set_defaults(func=_server_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
