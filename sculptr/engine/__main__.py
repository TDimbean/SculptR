from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .daemon import EngineDaemon
from ..process import EngineAlreadyRunning


def main() -> int:
    parser = argparse.ArgumentParser(prog="sculptr-engine")
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--models", required=True, type=Path)
    parser.add_argument("--nonce", required=True)
    parser.add_argument(
        "--client-pid",
        type=int,
        default=None,
        help="Process id of the UI client, so a stalled heartbeat is not read as a dead client",
    )
    arguments = parser.parse_args()
    daemon = EngineDaemon(
        arguments.session, arguments.models, arguments.nonce, client_pid=arguments.client_pid
    )
    try:
        daemon.run()
    except EngineAlreadyRunning as error:
        print(f"SculptR engine already running as pid {error.pid}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
