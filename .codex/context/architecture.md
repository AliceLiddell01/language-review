# Границы ответственности Python-пакета

- `cli.py` — точка входа CLI, аргументы и обработка кодов возврата; `__main__.py` — `python -m language_review`.
- `scanner.py` — обход, выбор, чтение, классификация файлов и защита путей; `report.py` — пользовательский и JSON-вывод.
- `extraction.py`, `extract_code.py`, `extract_docs.py`, `extract_structured.py`, `coordinates.py`, `extract_models.py` и `extract_report.py` реализуют read-only структурное извлечение; контракт описан в [extraction.md](extraction.md).
- `config.py` — загрузка, объединение настроек, профили, происхождение значений и маскирование; `doctor.py` — диагностика среды; `errors.py` — ошибки.
- `tests/` — контрактные тесты реального кода; `scripts/verify-package.ps1` — проверка установленного wheel/sdist в Windows.
- `pyproject.toml` определяет зависимости, минимальную версию Python, систему сборки, mypy/ruff/pytest; `uv.lock` фиксирует разрешённые зависимости; `.python-version` задаёт рабочую версию Python.

Языки внешних репозиториев не являются языками реализации пакета. Не добавляй Rust/C#/C++ build-инфраструктуру в этот Python-проект. Новые LLM/AST/AI backends допустимы только с реальной реализацией, границами отказов и тестами, а не как заглушки.
