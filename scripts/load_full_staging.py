"""Bounded dry-run loader for the original BGL and OpenStack archives."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import resource
import sys

from dotenv import load_dotenv

from log_insights.config import Settings
from log_insights.db import connect
from log_insights.full_ingestion import SelectionPolicy, load_archive

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load deterministic bounded subsets into full-v1 staging tables."
    )
    parser.add_argument("--bgl-stride", type=int, default=1000)
    parser.add_argument("--bgl-max-records", type=int, default=5000)
    parser.add_argument("--openstack-stride", type=int, default=20)
    parser.add_argument("--openstack-max-records-per-file", type=int, default=1000)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    settings = Settings.from_env()
    with connect(settings) as connection:
        bgl = load_archive(
            connection,
            dataset="bgl",
            target="staging",
            policy=SelectionPolicy(
                stride=args.bgl_stride,
                max_records_per_file=args.bgl_max_records,
                include_first_per_component=True,
            ),
        )
        openstack = load_archive(
            connection,
            dataset="openstack",
            target="staging",
            policy=SelectionPolicy(
                stride=args.openstack_stride,
                max_records_per_file=args.openstack_max_records_per_file,
                include_first_per_component=True,
            ),
        )
    max_rss_native = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    max_rss_bytes = (
        max_rss_native if sys.platform == "darwin" else max_rss_native * 1024
    )
    output = {
        "reports": [bgl.as_dict(), openstack.as_dict()],
        "process_metrics": {
            "max_resident_set_size_bytes": max_rss_bytes,
        },
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
