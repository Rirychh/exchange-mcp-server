"""Command-line entry point.

    exchange-mcp-server            run the MCP server over stdio (default)
    exchange-mcp-server check      test the configuration and print mailbox info
    exchange-mcp-server login      sign in once for EXCHANGE_AUTH=oauth_delegated
    exchange-mcp-server --version
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__
from .config import ConfigError, settings_from_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="exchange-mcp-server", description="MCP server for Microsoft Exchange (EWS).")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "check", "login"])
    parser.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http", "sse"],
                        help="MCP transport (default stdio).")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging to stderr.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")

    try:
        settings = settings_from_env()
        settings.validate()
    except ConfigError as exc:
        print(f"Configuration error: {exc}\nSee README.md (Configuration).", file=sys.stderr)
        return 2

    if args.command == "login":
        if settings.auth != "oauth_delegated":
            print("`login` is only needed for EXCHANGE_AUTH=oauth_delegated.", file=sys.stderr)
            return 2
        from .auth import MsalTokenStore

        store = MsalTokenStore(settings)
        store.login_device_code()
        print(f"Signed in. Token cache: {store.path}", file=sys.stderr)
        return 0

    if args.command == "check":
        from .backend import EwsBackend

        try:
            print(json.dumps(EwsBackend(settings).whoami(), ensure_ascii=False, indent=2, default=str))
        except Exception as exc:  # noqa: BLE001
            from .server import _friendly

            print(f"Connection failed: {_friendly(exc)}", file=sys.stderr)
            return 1
        return 0

    from .server import build_server

    build_server(settings).run(transport=args.transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
