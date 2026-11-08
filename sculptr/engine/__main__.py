from __future__ import annotations

import argparse
from pathlib import Path

from .daemon import EngineDaemon


def main() -> int:
    parser = argparse.ArgumentParser(prog="sculptr-engine")
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--models", required=True, type=Path)
    parser.add_argument("--nonce", required=True)
    arguments = parser.parse_args()
    EngineDaemon(arguments.session, arguments.models, arguments.nonce).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
