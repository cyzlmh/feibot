"""Programmatic gateway entrypoint (non-CLI command surface)."""

from __future__ import annotations

import argparse
from pathlib import Path

from feibot.cli import commands as cli_commands


def main() -> None:
    parser = argparse.ArgumentParser(description="Start feibot gateway runtime.")
    parser.add_argument("--config", required=True, help="Path to config file")
    parser.add_argument("--port", type=int, default=18790, help="Gateway port")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists() or not config_path.is_file():
        raise SystemExit(f"Config file not found: {config_path}")

    cli_commands._CONFIG_PATH = config_path
    cli_commands.gateway(
        port=args.port,
        verbose=args.verbose,
        debug=args.debug,
    )


if __name__ == "__main__":
    main()
