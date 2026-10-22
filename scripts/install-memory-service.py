"""Установка отдельного Windows worker из checkout либо проверенного комплекта."""

from language_review.memory.service import main

if __name__ == "__main__":
    import sys

    raise SystemExit(main(["install", *sys.argv[1:]]))
