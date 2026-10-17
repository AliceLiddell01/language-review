# Маршрутизация agent-critical контекста

`.codex/context/` хранит только долгоживущие, важные для реализации границы. Здесь нет истории PR, временных планов, копий README или руководств CLI.

| Область задачи | Контракт | Фактические владельцы |
| --- | --- | --- |
| Архитектура пакета / зависимости | [architecture.md](architecture.md) | `src/language_review/**`, `pyproject.toml` |
| Выбор файлов, `scan`, JSON и границы пути | [scanner.md](scanner.md) | `scanner.py`, `report.py`, `tests/test_scanner.py` |
| Конфигурация, profiles, `doctor` | [configuration.md](configuration.md) | `config.py`, `doctor.py`, `tests/test_config.py`, `tests/test_doctor.py` |
| Верификация, acceptance и инфраструктура | [verification.md](verification.md) | `pyproject.toml`, `scripts/verify-package.ps1`, `.github/workflows/**` |
| Git/GitHub | — | `.agents/skills/language-review-git-workflow/SKILL.md` |
| Ревью и CodeRabbit | — | `.agents/skills/language-review-code-review/SKILL.md`, `.agents/skills/language-review-coderabbit-review/SKILL.md` |

Не дублируй источники истины. Процедуры в `.agents/skills/`, объяснения и troubleshooting — в `README.md` и `docs/development-workflow.md`.
