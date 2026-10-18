# Проверки и доказательства

Все фактически применимые проверки запускаются на актуальном HEAD после интеграции изменений. Не подменяй проверки автономным имитатором логики продукта.

## Канонические проверки разработки в Windows

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -n 8
.\scripts\verify-package.ps1
```

Скрипт собирает wheel/sdist, устанавливает их в изолированные среды Windows и проверяет настоящий CLI, `python -m language_review`, JSON-вывод `scan` и структурное извлечение через `extract`.

## Проверки агентской и GitHub-инфраструктуры

```powershell
uv run --locked python .github/scripts/check_skill_contracts.py
uv run --locked python .github/scripts/check_local_data_untracked.py
uv run --locked python -m unittest discover -s .github/scripts -p 'test_*.py'
uv run --locked python .github/scripts/check_pr_body.py --body-file путь\к\pr-body.md
```

Проверяй файл с описанием PR только на реальном тексте чернового описания. GitHub Actions отдельно проверяет фактическое описание PR. Конфигурацию Renovate проверяет `.github/workflows/renovate-config.yml` официальным строгим валидатором. Перед публикацией проверяй YAML GitHub Actions и CodeRabbit, JSON Renovate и локальные ссылки; не создавай вторую реализацию разрешения зависимостей Python.

## Статусы

`PASS` — реально запущено и прошло; `FAIL` — запущено и не прошло; `PARTIAL` — проверена лишь часть; `NOT_RUN` — не запускалось. Указывай точную среду и недоказанные аспекты. CI в Ubuntu и анализ CodeQL не подтверждают работу в Windows. После изменения HEAD прежние результаты CI и CodeRabbit автоматически не переносятся.
