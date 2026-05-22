"""Run a single decision cycle. Use --dry-run to skip execution."""
from __future__ import annotations

import argparse
import logging

from hl_agent.main import run_one_cycle
from hl_agent.settings import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build context and call Claude, but do not send orders to the exchange.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    settings = load_settings()
    run_one_cycle(settings, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
