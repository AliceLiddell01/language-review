# Контракт предметной памяти

- Владельцы: `memory/`, `compose.memory.yml`, `scripts/*memory*`,
  `tests/test_memory*.py`. Эксплуатация и решения — [docs/memory.md](../../docs/memory.md).
- Память явно включается в `[memory]`/CLI и пишет отдельное состояние приложения.
  База не нужна для `scan`/`extract`/check без памяти; драйвер — optional `memory` extra.
- Каноничны события PostgreSQL, occurrences и решения. LLM-гипотезы и векторы производные.
  `accepted/rejected/disputed` поступают только через validated `decide-review`/submit_decision.
- Интеграция находится в `check_paths` до LLM и после review formation. Не переносить её
  только в CLI и не добавлять ingestion в `show-review`/`verify-review`.
- Source verification исторического импорта принадлежит существующему `review_verify`.
  Runtime report IDs не являются постоянным ключом. Project UUID задаётся явно.
- Primary events/jobs коммитятся транзакционно. Ошибка записи — exit 6 без успешного
  отчёта; started-run не считается завершённым. Retry/lease/fencing не создают решений.
- `memory work` требует явный `--project` либо операторский `--all-projects`.
  Общий worker ограничивает jobs/embedding и обнаруживает новые проекты из БД.
  Windows deployment — `memory/service.py` и `scripts/install-memory-service.py`:
  отдельный wheel/runtime,
  DPAPI credentials, пользовательская logon task; checkout не нужен процессу.
  Обновление фиксируется только после readiness задачи, иначе откатываются файлы runtime
  и задача; миграции БД не откатываются. Supervisor вызывает `compose up` только при
  `memory_database_failed` и остановленном по данным Docker собственном PostgreSQL.
- LLM-гипотеза продолжает активную ветвь с общим свидетельством той же
  category/relation/dataset; объединённые ветви становятся `superseded` с историей.
- Recall фильтруется по области, provenance/dataset и состоянию; global требует явного
  public_rule. Автоматический контекст не содержит private payload другого проекта.
- Основной LLM prompt версионируется existing PROMPT_VERSION/fingerprint; отдельный
  prompt/schema консолидации не назначает истину. Existing source/protected checks обязательны.
- Test/holdout не обрабатываются и не индексируются. Evaluator capture маркирует split;
  synthetic knowledge выбирается явно, dev не обучает свои последующие повторы.
- Cloud consent для check не разрешает фоновую консолидацию: worker отклоняет ChatGPT до claim. Передача ограниченного recall в облачный check требует отдельного allow_memory; snapshot сохраняется хешем.
- Доказательство live БД/модели не заменяется JsonServer. Общие команды проверки принадлежат
  [verification.md](verification.md); отдельная приёмка — `scripts/verify-memory.py`.
