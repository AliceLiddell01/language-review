# Границы ответственности Python-пакета

- `cli.py` — точка входа CLI, аргументы и обработка кодов возврата; `__main__.py` — `python -m language_review`.
- `scanner.py` — обход, выбор, чтение, классификация файлов и защита путей; `report.py` — пользовательский и JSON-вывод.
- `extraction.py`, `extract_code.py`, `extract_docs.py`, `extract_structured.py`, `coordinates.py`, `extract_models.py` и `extract_report.py` реализуют структурное извлечение только для чтения; контракт описан в [extraction.md](extraction.md).
- `check.py`, `check_models.py`, `check_report.py`, `masking.py`, `engine_http.py`, `engine_languagetool.py`, `engine_vale.py`, `engine_llm.py` и данные `vale_styles/` реализуют языковую проверку; контракт описан в [check.md](check.md).
- `review_report.py` представляет тот же результат проверки для агента; `review_verify.py` валидирует импорт по упакованной `review-report.schema.json` через `jsonschema`, сверяет сохранённые цитаты и хеши с явно указанными исходниками. Контракт — в [check.md](check.md).
- `config.py` — загрузка, объединение настроек, профили, происхождение значений и маскирование; `doctor.py` — диагностика среды; `errors.py` — ошибки.
- `tests/` — контрактные тесты реального кода; `scripts/verify-package.ps1` — проверка установленного wheel/sdist в Windows.
- `pyproject.toml` определяет зависимости, минимальную версию Python, систему сборки, mypy/ruff/pytest; `uv.lock` фиксирует разрешённые зависимости; `.python-version` задаёт рабочую версию Python.

Языки внешних репозиториев не являются языками реализации пакета. Не добавляй Rust/C#/C++ build-инфраструктуру в этот Python-проект. Новые LLM/AST/AI backends допустимы только с реальной реализацией, границами отказов и тестами, а не как заглушки.
