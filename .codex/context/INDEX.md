# Маршрутизация критически важного контекста для реализации

`.codex/context/` хранит только долгосрочные границы, важные для реализации. Здесь нет истории PR, временных планов, копий README или руководств CLI.

| Область задачи | Контракт | Фактические владельцы |
| --- | --- | --- |
| Архитектура пакета / зависимости | [architecture.md](architecture.md) | `src/language_review/**`, `pyproject.toml` |
| Выбор файлов, `scan`, JSON и границы пути | [scanner.md](scanner.md) | `scanner.py`, `report.py`, `tests/test_scanner.py` |
| Структурное извлечение, координаты и схема отчёта | [extraction.md](extraction.md) | `extraction.py`, `extract_*.py`, `coordinates.py`, `docs/extract-report.schema.json`, `docs/extract-report.example.json`, `tests/test_extract.py` |
| Языковая проверка `check`, review для агента, движки, маскирование | [check.md](check.md) | `check.py`, `check_*.py`, `review_*.py`, `engine_*.py`, `masking.py`, `vale_styles/`, `docs/check-report.*.json`, `docs/review-*.json`, `tests/test_check*.py`, `tests/test_review.py`, `tests/test_masking.py` |
| Конфигурация, профили, `doctor` | [configuration.md](configuration.md) | `config.py`, `doctor.py`, `tests/test_config.py`, `tests/test_doctor.py` |
| Предметная память, очередь, решения и гибридный recall | [memory.md](memory.md) | `memory/`, `compose.memory.yml`, `scripts/*memory*`, `tests/test_memory*.py` |
| Независимая оценка, корпус, offline scoring и регрессии | [evaluation.md](evaluation.md) | `evaluation/**`, `docs/evaluation.md`, `tests/test_evaluation*.py` |
| Проверки, приемка и инфраструктура | [verification.md](verification.md) | `pyproject.toml`, `scripts/verify-package.ps1`, `.github/workflows/**` |
| Релизы, установка и глобальные пользовательские skills | [distribution.md](distribution.md) | `release_contract.py`, `release_transport.py`, `release_acceptance.py`, `distribution.py`, `setup_cli.py`, `scripts/release.py`, `scripts/install.ps1`, `scripts/verify-distribution.py`, `.github/workflows/release.yml` |
| Git/GitHub | — | `.agents/skills/language-review-git-workflow/SKILL.md` |
| Ревью и CodeRabbit | — | `.agents/skills/language-review-code-review/SKILL.md`, `.agents/skills/language-review-coderabbit-review/SKILL.md` |

Не дублируй источники истины. Процедуры находятся в `.agents/skills/`, объяснения и устранение неполадок — в `README.md` и `docs/development-workflow.md`.
