# Границы ответственности Python-пакета

- `cli.py` — CLI entrypoint, аргументы и обработка кодов возврата; `__main__.py` — `python -m language_review`.
- `scanner.py` — обход, выбор, чтение, классификация файлов и защита путей; `report.py` — пользовательский и JSON-вывод.
- `config.py` — загрузка, merge, профили, provenance и masking; `doctor.py` — диагностика окружения; `errors.py` — ошибки.
- `tests/` — контрактные тесты реального кода; `scripts/verify-package.ps1` — Windows smoke установленного wheel/sdist.
- `pyproject.toml` определяет зависимости, Python floor, build backend, mypy/ruff/pytest; `uv.lock` фиксирует разрешённые зависимости; `.python-version` определяет runtime.

Языки внешних репозиториев не являются языками реализации пакета. Не добавляй Rust/C#/C++ build-инфраструктуру в этот Python-проект. Новые LLM/AST/AI backends допустимы только с реальной реализацией, границами отказов и тестами, а не как заглушки.
