"""Entry point for ``python -m losesound``."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .config import ConfigStore
from .http_server import build_context, create_server
from .log import log
from .net import prefer_ipv4


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LoseSound preset and stream helper for Bose SoundTouch")
    parser.add_argument("--host", default=os.getenv("LISTEN_ADDR", "0.0.0.0"), help="listen address (default: 0.0.0.0)")
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("LISTEN_PORT", "8092")), help="listen port (default: 8092)"
    )
    parser.add_argument("--config", default=os.getenv("CONFIG_PATH", "/data/config.json"), help="configuration file")
    parser.add_argument("--version", action="version", version=f"LoseSound {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    prefer_ipv4()
    store = ConfigStore(args.config)
    store.load()
    context = build_context(store)
    server = create_server(context, args.host, args.port)
    log(
        "proxy.start",
        version=context.version,
        host=args.host,
        port=args.port,
        base_url=context.base_url,
        aftertouch_url=context.aftertouch_url,
        speaker_ip=context.speaker_ip,
        speaker_ips=",".join(context.speaker_ips),
        speaker_port=context.speaker_port,
        config=str(store.path),
        api_token_enabled=bool(context.api_token),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("proxy.stop", reason="keyboard_interrupt")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
