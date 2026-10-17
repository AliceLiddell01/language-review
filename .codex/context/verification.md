# Verification и доказательства

Все фактически применимые проверки запускаются на актуальном HEAD после интеграции пишущих агентов. Не подменяй проверки автономным имитатором логики продукта.

## Windows development gate (канонический)

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -n 8
.\scripts\verify-package.ps1
```

Smoke script собирает wheel/sdist, устанавливает их в изолированные окружения Windows, вызывает настоящий CLI, `python -m language_review`, JSON `scan`.

## Agent/GitHub infrastructure gate

```powershell
python .github/scripts/check_skill_contracts.py
python -m unittest discover -s .github/scripts -p 'test_*.py'
python .github/scripts/check_pr_body.py --body-file path\to\pr-body.md
```

PR-body-validator на файле запускай только для реального draft body. GitHub Actions проверяет фактическое тело PR отдельно. GitHub Actions YAML, CodeRabbit YAML, Renovate JSON и локальные ссылки проверяй перед публикацией; не вноси вторую реализацию Python dependency resolution.

## Статусы

`PASS` — реально запущено и прошло; `FAIL` — запущено и не прошло; `PARTIAL` — проверена лишь часть; `NOT_RUN` — не запускалось. Указывай точное окружение и недоказанные аспекты. CI Ubuntu и CodeQL анализ не подтверждают Windows runtime. После изменения HEAD прежние CI/CodeRabbit свидетельства не переносятся автоматически.
