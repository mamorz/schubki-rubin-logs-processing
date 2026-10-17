"""Stream and inspect every record in the official full-data archives."""

from __future__ import annotations

import json

from log_insights.full_ingestion import inspect_archives


def main() -> None:
    report = {
        dataset: inspection.as_dict()
        for dataset, inspection in inspect_archives().items()
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
