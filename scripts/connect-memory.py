"""Запуск программы с DPAPI-учётными данными только в окружении дочернего процесса."""

from language_review.memory.service import main

if __name__ == "__main__":
    import sys

    raise SystemExit(main(["connect", *sys.argv[1:]]))
