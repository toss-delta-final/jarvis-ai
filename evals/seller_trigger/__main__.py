"""`uv run python -m evals.seller_trigger` 진입점."""

from __future__ import annotations

import sys

from evals.seller_trigger.cli import main

if __name__ == "__main__":
    sys.exit(main())
