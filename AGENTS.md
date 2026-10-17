# Language Review — обязательные правила агента

Language Review — локальная Python CLI-утилита для инвентаризации текстовых файлов в произвольных репозиториях. Краткое состояние продукта — в `README.md`. Этот документ — только глобальные инварианты и маршрутизация, не копия технического справочника.

## Глобальные инварианты

- Рабочий runtime — Windows/PowerShell/Python 3.15. Если checkout находится в Windows, команды разработки, Git и проверки выполняй в этой среде нативными инструментами; самовольная замена на WSL2 запрещена. Дополнительный Linux CI не заменяет Windows acceptance.
- Репозитории Rust, C#, C++ и документация — внешние входные данные. Не встраивай в них языковые предположения или зависимости Language Review.
- `scan` пока выполняет инвентаризацию, а не поиск языковых ошибок; `check`, LLM, LanguageTool, Vale, SARIF, AST, Git diff и применение исправлений не реализованы. Не выдавай планы за функции.
- `scan`, `doctor`, `config show` не меняют исследуемые исходники. Явный `scan --output` создаёт только отчёт. Сохраняй границы файловой системы, обработку символических ссылок, разделение stdout/stderr, redaction секретов, JSON-схемы и exit codes.
- Фактические версии и зависимости принадлежат `pyproject.toml`, `.python-version` и `uv.lock`; lock обновляй только через uv. Не публикуй machine-specific абсолютные пути, чужие исходники, логи, токены, конфиги с секретами или пользовательские результаты.
- Project-owned тексты и документация преимущественно на русском; внешние API, имена символов, аргументов и точные идентификаторы не переводить.
- Не создавай абстракции, конфигурацию и проверки для несуществующих возможностей. Тесты подтверждают реальные entrypoints и наблюдаемый контракт; не подменяй реализацию и не ослабляй assertions.
- Одна публикуемая задача — одна ветка и PR. После изменения подтверждай точный удалённый HEAD. Merge — только по отдельной текущей явной команде. CodeRabbit CLI запускай только по явному положительному запросу.

## Как получать контекст

1. Установи затронутую подсистему по коду и задаче.
2. Открой `.codex/context/INDEX.md` и только релевантный документ. Код, manifests, тесты и runtime evidence имеют приоритет над прозой.
3. Перед процедурой проверь соответствующий skill ниже и прочитай его `SKILL.md`. Не загружай все references заранее.

| Намерение | Владелец |
| --- | --- |
| Архитектура, инвентаризация, конфигурация, verification | `.codex/context/INDEX.md` |
| Координация субагентов | `.agents/skills/language-review-agent-coordination/SKILL.md` |
| Ветка / Git / commit / push / PR / merge | `.agents/skills/language-review-git-workflow/SKILL.md` |
| Независимое код-ревью | `.agents/skills/language-review-code-review/SKILL.md` |
| Явно запрошенные итерации CodeRabbit | `.agents/skills/language-review-coderabbit-review/SKILL.md` |
| Настоящая smoke/acceptance-проверка | `.agents/skills/language-review-live-testing/SKILL.md` |
| Размещение нового знания/документации | `.agents/skills/language-review-documentation/SKILL.md` |

## Базовая verification (PowerShell, из корня)

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -n 8
.\scripts\verify-package.ps1
```

Проверки агентской инфраструктуры и PR: см. `.codex/context/verification.md`. Указывай PASS/FAIL/PARTIAL/NOT_RUN и фактическое окружение; не называй незапущенные проверки успешными.
