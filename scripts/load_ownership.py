"""Load the versioned fictional component ownership mapping into SereneDB."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from dotenv import load_dotenv

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.ownership import DEFAULT_OWNERSHIP_PATH, load_ownership_mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load the v1 fictional component-to-team ownership mapping."
    )
    parser.add_argument("--mapping", type=Path, default=DEFAULT_OWNERSHIP_PATH)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    settings = Settings.from_env()
    with connect(settings) as connection:
        result = load_ownership_mapping(connection, args.mapping)
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
