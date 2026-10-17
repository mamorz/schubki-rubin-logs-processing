"""Run: python -m log_analysis.check. Never print connection exceptions/secrets."""
import json
import sys
from .db import health_check


def main():
    try:
        print(json.dumps(health_check(), ensure_ascii=False, default=str))
    except Exception as exc:
        print(f"Connection check failed ({type(exc).__name__}). Check environment, credentials and port 7891.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
