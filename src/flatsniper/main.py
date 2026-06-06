"""Entrypoint / scheduler loop.

Milestone 0: only `--check-config` is implemented. The async polling loop lands
in Milestone 5.
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import ConfigError, load_config


def _check_config() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Config error:\n{exc}", file=sys.stderr)
        return 1
    print("FlatSniper config loaded OK. (secrets redacted)\n")
    print(json.dumps(config.public_dict(), indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flatsniper", description="Munich flat-hunting bot")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Load and print the (non-secret) config, then exit.",
    )
    args = parser.parse_args(argv)

    if args.check_config:
        return _check_config()

    print("Scheduler loop not implemented yet (Milestone 5). Try --check-config.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
