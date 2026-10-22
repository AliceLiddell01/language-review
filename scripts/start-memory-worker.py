"""Безоконный запуск ограниченного одним проектом worker с журналами."""

from language_review.memory.service import main

if __name__ == "__main__":
    import sys

    raise SystemExit(main(["start-worker", *sys.argv[1:]]))
