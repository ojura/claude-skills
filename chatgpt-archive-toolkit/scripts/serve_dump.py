#!/usr/bin/env python3
"""Serve a ChatGPT archive directory on localhost."""

from __future__ import annotations

import argparse
import functools
import http.server
import socket
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump_dir", help="ChatGPT archive/dump directory")
    parser.add_argument("--port", type=int, default=8877, help="starting port")
    parser.add_argument("--host", default="127.0.0.1", help="bind host")
    return parser.parse_args()


def available_port(host: str, start: int) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no available port from {start} to {start + 99}")


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))


def main() -> int:
    args = parse_args()
    root = Path(args.dump_dir).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"dump directory does not exist: {root}")

    port = available_port(args.host, args.port)
    handler = functools.partial(Handler, directory=str(root))
    server = http.server.ThreadingHTTPServer((args.host, port), handler)
    url = f"http://{args.host}:{port}/browser/index.html"
    print(f"serving {root}")
    print(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
