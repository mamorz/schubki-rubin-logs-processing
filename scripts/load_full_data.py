"""Explicitly gated loader for the complete original BGL/OpenStack corpora."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.full_ingestion import SelectionPolicy, load_archive

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load complete original logs into separate full-v1 tables."
    )
    parser.add_argument(
        "--dataset",
        choices=("bgl", "openstack", "both"),
        default="both",
    )
    parser.add_argument(
        "--confirm-full-load",
        action="store_true",
        help="Required acknowledgement that the reviewed multi-million-row load is intended.",
    )
    args = parser.parse_args()
    if not args.confirm_full_load:
        parser.error(
            "refusing complete load without --confirm-full-load after dry-run review"
        )
    return args


def main() -> None:
    args = _parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    settings = Settings.from_env()
    datasets = ("bgl", "openstack") if args.dataset == "both" else (args.dataset,)
    reports = []
    with connect(settings) as connection:
        for dataset in datasets:
            reports.append(
                load_archive(
                    connection,
                    dataset=dataset,
                    target="full",
                    policy=SelectionPolicy(),
                    strict=True,
                ).as_dict()
            )
    print(json.dumps({"reports": reports}, indent=2))


if __name__ == "__main__":
    main()
