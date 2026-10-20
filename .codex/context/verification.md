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

Скрипт собирает wheel/sdist, устанавливает их в изолированные среды Windows и проверяет настоящий CLI, `python -m language_review`, JSON-вывод `scan`, структурное извлечение через `extract`, `check` со встроенными правилами Vale (без Vale — ожидаемый статус `unavailable` и код 5), review-контракт, `show-review`, `verify-review` и сохранность исходного файла после каждой команды чтения.

Оценочный workflow дополнительно проверяется через установленный `language-review-eval`
и `python -m language_review.evaluation`: импорт корпуса/сохранённого review,
намеренный PARTIAL синтетической фикстуры, контрольные TP/FN, детерминированные ожидания,
compare и неизменность входов. `tests/test_evaluation*.py` не требуют живых сервисов.
Явный live capture и границы независимого baseline описаны в [руководстве](../../docs/evaluation.md).

`tests/test_review.py` сверяет упакованную и документированную review-схемы, вложенный контракт check, отказ обеих команд при повреждённом JSON и согласованность локализаций `items`/`analysis`.

## Приёмка языковых движков

Контрактные тесты `tests/test_check.py` используют локальные HTTP-серверы протоколов и проверяют отказы; они не доказывают работу настоящих движков. `tests/test_check_live.py` запускается на реальных LanguageTool, `llama-server`, Ollama и Vale только при заданных `LANGUAGE_REVIEW_LIVE_LANGUAGETOOL`, `LANGUAGE_REVIEW_LIVE_LLAMA_CPP` (`_MODEL`), `LANGUAGE_REVIEW_LIVE_OLLAMA` (`_MODEL`) и найденном `vale`; пропуск — `NOT_RUN`. Эталон `docs/check-report.example.json` перестраивается фактическим CLI на входах `prepare_example_inputs` из `tests/test_check_live.py`. Модели, журналы движков и пользовательские тексты не добавляются в Git.

## Проверки агентской и GitHub-инфраструктуры

```powershell
uv run --locked python .github/scripts/check_skill_contracts.py
uv run --locked python .github/scripts/check_local_data_untracked.py
uv run --locked python -m unittest discover -s .github/scripts -p 'test_*.py'
uv run --locked python .github/scripts/check_pr_body.py --body-file путь\к\pr-body.md
```

Проверяй файл с описанием PR только на реальном тексте чернового описания. GitHub Actions отдельно проверяет фактическое описание PR. Конфигурацию Renovate проверяет `.github/workflows/renovate-config.yml` официальным строгим валидатором. Перед публикацией проверяй YAML GitHub Actions и CodeRabbit, JSON Renovate и локальные ссылки; не создавай вторую реализацию разрешения зависимостей Python.

## Статусы

Предметная память: `scripts/verify-memory.ps1` запускает изолированную PostgreSQL/pgvector
через Docker Desktop, pytest с восемью workers, restart/recreate и реальный backup →
потерю volume → restore с digest-проверкой таблиц и vector retrieval. Live LLM/embedding
включаются отдельными аргументами; без них соответствующие проверки NOT_RUN. Подробности
и команды — [docs/memory.md](../../docs/memory.md). CI `memory-integration` проверяет
реальную PostgreSQL в Ubuntu, не заменяя приемку Windows.

`PASS` — реально запущено и прошло; `FAIL` — запущено и не прошло; `PARTIAL` — проверена лишь часть; `NOT_RUN` — не запускалось. Указывай точную среду и недоказанные аспекты. CI в Ubuntu и анализ CodeQL не подтверждают работу в Windows. После изменения HEAD прежние результаты CI и CodeRabbit автоматически не переносятся.
