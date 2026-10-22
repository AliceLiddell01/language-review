"""Управление собственной PostgreSQL: up/status/stop/backup/restore."""

import sys
from pathlib import Path

from language_review.memory.database import main

if __name__ == "__main__":
    raise SystemExit(
        main(
            [
                *sys.argv[1:],
                "--compose-file",
                str(Path(__file__).resolve().parents[1] / "compose.memory.yml"),
            ]
        )
    )
