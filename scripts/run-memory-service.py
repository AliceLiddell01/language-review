"""Supervisor переднего плана для отдельной установки worker."""

from language_review.memory.service import main

if __name__ == "__main__":
    import sys

    raise SystemExit(main(["supervise", *sys.argv[1:]]))
