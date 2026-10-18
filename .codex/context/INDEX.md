# Маршрутизация критически важного контекста для реализации

`.codex/context/` хранит только долгосрочные границы, важные для реализации. Здесь нет истории PR, временных планов, копий README или руководств CLI.

| Область задачи | Контракт | Фактические владельцы |
| --- | --- | --- |
| Архитектура пакета / зависимости | [architecture.md](architecture.md) | `src/language_review/**`, `pyproject.toml` |
| Выбор файлов, `scan`, JSON и границы пути | [scanner.md](scanner.md) | `scanner.py`, `report.py`, `tests/test_scanner.py` |
| Структурное извлечение, координаты и схема отчёта | [extraction.md](extraction.md) | `extraction.py`, `extract_*.py`, `coordinates.py`, `docs/extract-report.schema.json`, `docs/extract-report.example.json`, `tests/test_extract.py` |
| Конфигурация, профили, `doctor` | [configuration.md](configuration.md) | `config.py`, `doctor.py`, `tests/test_config.py`, `tests/test_doctor.py` |
| Проверки, приемка и инфраструктура | [verification.md](verification.md) | `pyproject.toml`, `scripts/verify-package.ps1`, `.github/workflows/**` |
| Git/GitHub | — | `.agents/skills/language-review-git-workflow/SKILL.md` |
| Ревью и CodeRabbit | — | `.agents/skills/language-review-code-review/SKILL.md`, `.agents/skills/language-review-coderabbit-review/SKILL.md` |

Не дублируй источники истины. Процедуры находятся в `.agents/skills/`, объяснения и устранение неполадок — в `README.md` и `docs/development-workflow.md`.
