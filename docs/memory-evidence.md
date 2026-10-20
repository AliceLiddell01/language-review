# Доказательства реализации памяти

Среда: Windows, PowerShell 7, Python 3.15.0, Docker Desktop 4.95.0,
Docker Engine 29.8.2, Compose v5.6.0. Проверки выполняются на синтетических
данных вне checkout; исходники сторонних проектов не использовались.
Подробная процедура: [memory.md](memory.md).

## Проверяемые сценарии

| Требование | Доказательство |
| --- | --- |
| Автоматический сбор, UTF-8/UTF-16, BOM/CRLF, отдельные цитаты в разных файлах | `test_auto_ingestion_keeps_utf8_utf16_documents_evidence_and_distinct_locations` |
| Повторная и конкурентная доставка | `test_replay_new_runs_and_concurrent_delivery_never_duplicate_findings_or_jobs` |
| Решения, отрицательные случаи, конкуренция и история | `test_explicit_decisions_are_idempotent_revisioned_and_negative_cases_remain_searchable`, `test_concurrent_decisions_require_one_content_revision_and_isolate_projects` |
| Проверяемый historical import | `test_import_uses_existing_source_verifier_and_idempotent_provenance`, `test_conflicting_delivery_rolls_back_without_erasing_original` |
| Реальный CLI → решение → автоматический recall | `test_live_public_review_decision_and_automatic_next_recall` (Ollama) |
| Автоматическая инкрементальная группировка без повышения до нормы | `test_related_cases_form_one_incremental_hypothesis_and_retain_each_original`, `test_live_local_llm_consolidates_real_queue_without_promoting_truth` |
| Защита исходников и недоверенного LLM-вывода | `test_automatic_recall_reaches_real_llm_transport_and_keeps_protected_tokens`, `test_memory_keeps_ambiguous_and_protected_outputs_as_rejections`, `test_llm_failure_preserves_events_and_observable_retry` |
| Изоляция проекта и явная публикация общей нормы | `test_global_scope_requires_reviewed_public_text_and_never_exports_private_context` |
| Сбой модели и восстановление worker | `test_real_unavailable_llm_endpoint_keeps_events_and_never_reports_clean`, `test_worker_fencing_recovery_and_hypotheses_never_promote_observations`, `test_retry_budget_and_recovery_are_observable` |
| Настоящий pgvector, pinned embedding и rebuild | `test_live_pgvector_search_readback_is_scoped_and_versioned` |
| Семантика при отсутствии общих слов | `test_semantic_retrieval_finds_case_without_shared_terms_in_real_pgvector` — настоящий pgvector, протокольный embedding; отдельная live-проверка использует настоящую модель |
| Миграции и карантин holdout | `test_schema_migration_is_idempotent_and_detects_drift`, `test_holdout_events_are_quarantined_from_worker_index_and_decisions` |
| Restart/recreate и реальный backup → потеря volume → restore | `scripts/verify-memory.ps1`: digest всех 13 таблиц и pgvector distance read-back до/после |
| Настоящий отказ БД, базовый check без Docker | `scripts/verify-memory.ps1`: остановка контейнера, exit 6/пустой stdout, затем `--no-memory` |
| Wheel/sdist и optional extra | `scripts/verify-package.ps1`: установленные CLI/модуль/ресурсы, отдельная установка `[memory]` |

JsonServer и `ConsolidationProvider` проверяют протокол и ошибочные ветки;
они **не** считаются live-моделью. Тесты с `live` обращаются к настоящему
Ollama/pgvector только при явно заданных переменных среды. Полный pytest без
этих переменных имеет пропуски; отдельная Docker-приёмка включает их явно.

## Embedding: измеренный выбор

`scripts/benchmark-memory.py` содержит 8 синтетических passages и 8 заранее заданных
retrieval queries: русская орфография, project exception, JSON, deployment,
межпоточная блокировка, HTTP timeout, exit code, UTF-16/BOM.
Запросы не содержат ответов и меток scoring; ни один test/holdout оценщика не использован.

| Модель | Размерность | Recall@1 / @3 | MRR | Первый измеренный batch passages+queries |
| --- | --- | --- | --- | --- |
| EmbeddingGemma 300M | 768 | 1.0 / 1.0 | 1.0 | 2762 ms |
| Qwen3 Embedding 0.6B | 1024 | 1.0 / 1.0 | 1.0 | 10361 ms |

Digest EmbeddingGemma:
`85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1`;
Qwen3:
`ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d`.
Использованы официальные asymmetric retrieval prefixes. При одинаковом результате
маленького набора выбран более быстрый вариант. Это один прогон, не benchmark
пропускной способности и не доказательство превосходства на независимом корпусе.
Описание мультиязычных моделей: [EmbeddingGemma paper](https://arxiv.org/abs/2509.20354),
[Qwen3 Embedding](https://ollama.com/library/qwen3-embedding).

## Hindsight и прямой конвейер

Проверены официальный [репозиторий](https://github.com/vectorize-io/hindsight),
[Quick Start/API](https://hindsight.vectorize.io/developer/api/quickstart),
[configuration](https://hindsight.vectorize.io/developer/configuration) и
[retain](https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/docs/developer/retain.md).
Open-source лицензия репозитория — MIT; это самостоятельный сервер с LLM,
embedding, reranking и собственным storage/worker. Тексты отправляются выбранным
провайдерам. В эксперименте использованы только локальные Ollama endpoints;
существующий пользовательский Hindsight и его provider не изменялись.

Первый изолированный эксперимент использовал имеющийся образ с API 0.10.3:
`sha256:b5da1a9aece1f53a15f12821fb1470f358ed6c74dfaa0c4b1a39c3f3ae93e002`.
LLM — Qwen3.5 4B, embeddings — EmbeddingGemma, reranker — поддерживаемый `rrf`.
`none` этим образом не поддерживается: первоначальный запуск завершился ошибкой
конфигурации, после чего выбран документированный поддерживаемый backend.

8 документов → completed async retain → 8 извлечённых memory units, по одному
на документ. Повтор того же `operation_id` вернул прежнюю операцию. Все 8 запросов
вернули нужный document на первой позиции; полный retain+8 recall занял 39772 ms.
Это включает извлечение и поиск, поэтому время нельзя напрямую сравнивать со строками
embedding-таблицы. Отрицательное решение сохранило смысл «endpoint допустим», но
не предметные enum/status/revision; UTF-16/BOM был возвращён английским обобщением.

Проверка releases отдельно обнаружила актуальный `v1.0.0` от 2026-10-13. На нём
эксперимент повторён с image digest
`a88678c038b0e4340822b02a5efc566c56d8176b5bffa731bc433fd37f978e4c`,
API подтвердил версию 1.0.0. Результат: **8 документов / 8 facts / 0 zero-fact documents**,
8/8 нужных документов на первом месте, повтор operation_id идемпотентен;
retain+8 recall — **23211 ms**. Enum/status/revision предметного решения по-прежнему
не возвращаются; английское обобщение UTF-16/BOM формулируется как «were rejected as
exact technical terms», хотя отклонено замечание о переводе, а не сами термины.

Воспроизведение с теми же локальными моделями, новым изолированным контейнером и bank:

```powershell
.\scripts\experiment-hindsight.ps1 -Output $newExternalBenchmarkJson
```

Hindsight 1.0.0 проверен как кандидат. Production-adapter не добавлен; существующий
пользовательский контейнер не участвовал в эксперименте. Полное восстановление и
массовая нагрузка Hindsight остаются NOT_RUN.

| Критерий | Прямой PostgreSQL-конвейер | Hindsight как дополнительный слой |
| --- | --- | --- |
| Русский retrieval | Измерен на указанном наборе; case status не преобразуется | Первый эксперимент нашёл документы, extraction меняет представление |
| Идемпотентность | Транзакционные run/finding/event/decision/job keys, concurrency tests | Async operation_id проверен; предметные решения потребовали бы внешнего реестра |
| Отрицательные решения / traceability | Enum, причина, ревизия, exact occurrence, исходный review | Семантический текст и document links; отдельного предметного контракта нет |
| Сбой / restore | Реальные Windows Docker backup/loss/restore и fault checks | Восстановление Hindsight отдельно NOT_RUN |
| Нагрузка | Bounded incremental worker; массовая нагрузка NOT_RUN | Отдельные LLM extraction, consolidation, индексы и синхронизация |
| Эксплуатация | Одна каноническая БД + нативный worker | Ещё один сервер/банк и согласование производных данных |

Решение: Hindsight не включён в runtime-зависимости. Наблюдаемого преимущества,
которое оправдывает второй источник производных данных и его синхронизацию, не
установлено. Это ограниченное инженерное решение, не утверждение о худшем качестве
Hindsight вообще. Его retain receipt не является доказательством доступности знания:
официальный retain contract допускает **completed с нулём facts**, после чего
document не находится через recall/reflect. Benchmark отдельно читает documents
и выполняет recall, не ограничиваясь success HTTP.

## Границы оценки

На неизменённом `tests/fixtures/evaluation/corpus.json`, split `dev`, выполнены
три настоящих `language-review-eval run` через Ollama Qwen3.5 4B, по одному повтору.
Синтетический учебный опыт хранится отдельно с `knowledge_dataset="synthetic"`;
два содержательных синтетических решения переданы через обычный `decide-review`.
Gold оценщика не импортируется в память. Результаты сопоставлены штатным `compare`.

| Режим | Pipeline TP / FP / FN | Precision | Recall | Статус оценщика |
| --- | --- | --- | --- | --- |
| Без памяти | 6 / 0 / 5 | 1.0 | 0.54545 | PARTIAL |
| Подтверждённая структурная память | 6 / 0 / 5 | 1.0 | 0.54545 | PARTIAL |
| Гибридный поиск | 6 / 0 / 5 | 1.0 | 0.54545 | PARTIAL |

Языкового улучшения этот запуск **не показал**. PARTIAL отражает границы покрытия
имеющегося синтетического корпуса и не переименован в PASS. На двух memory-прогонах
автоматически сохранены все 12 локализованных наблюдений; вместе с двумя учебными
прогонами и решениями получены 34 durable events, 8 отдельных случаев и 2 accepted case
rules с проверенными векторами. Повторные наблюдения не стали новыми подтверждениями.

Для воспроизведения трёх режимов используйте внешний TOML с одним и тем же project UUID,
provider/model, сначала `memory.enabled=false`, затем `recall_mode="structured"` и
`"hybrid"`. Нужен новый внешний output для каждой команды:

```powershell
uv run --locked --extra memory language-review-eval run tests/fixtures/evaluation/corpus.json `
    --split dev --engine llm --config $modeConfig --product-commit $testedCommit --output $newOutput
uv run --locked language-review-eval compare $baselineEvaluation $candidateEvaluation --format json
```

Независимый экспертный baseline на реальном русском техническом корпусе — **NOT_RUN**.
Синтетический корпус существующего `language-review-eval` сохраняет собственную
разметку и matching; сравнение memory disabled/structured/hybrid не меняет gold.
Даже если числа улучшаются, это не независимая оценка языкового качества.
Массовая нагрузка, ANN-оптимизация, авторизация нескольких недоверенных операторов,
криптографическое удостоверение reviewer и гарантированное обнаружение произвольных
секретов — не доказаны данным запуском.

Итоговая проверка runtime commit `199b62ca1989dbdbb0518b84a2f9514de90a582e`:
полный Windows pytest — **515 passed, 12 skipped**; отдельная Docker-приёмка с
реальными Ollama/EmbeddingGemma — **51 passed**, restart/recreate/backup-loss-restore
всех 13 таблиц и двух векторов — PASS. Wheel и sdist после установки optional extra
успешно выполнили `memory health` на настоящей БД. Ruff, mypy, actionlint и проектные
валидаторы прошли. Скрытый worker отдельно запущен в Windows и остановлен вместе с
его process tree; Python CLI и исходники от этой проверки не изменились.
