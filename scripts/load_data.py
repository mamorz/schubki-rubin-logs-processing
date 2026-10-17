"""Load the two public Loghub 2,000-row structured samples into SereneDB."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.ingestion import EXPECTED_SAMPLE_ROWS, load_samples, validation_report

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BGL_PATH = PROJECT_ROOT / "data" / "raw" / "BGL_2k.log_structured.csv"
DEFAULT_OPENSTACK_PATH = (
    PROJECT_ROOT / "data" / "raw" / "OpenStack_2k.log_structured.csv"
)


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load the public Loghub BGL and OpenStack 2k CSV samples."
    )
    parser.add_argument("--bgl", type=Path, default=DEFAULT_BGL_PATH)
    parser.add_argument("--openstack", type=Path, default=DEFAULT_OPENSTACK_PATH)
    parser.add_argument("--expected-rows", type=int, default=EXPECTED_SAMPLE_ROWS)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    settings = Settings.from_env()

    with connect(settings) as connection:
        result = load_samples(
            connection,
            bgl_path=args.bgl,
            openstack_path=args.openstack,
            expected_rows=args.expected_rows,
        )
        report = validation_report(connection)

    output = {
        "loaded": {
            "bgl": result.bgl_rows,
            "openstack": result.openstack_rows,
        },
        "validation": report,
    }
    print(json.dumps(output, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
