# Предметная память языковых решений

Память включается явно. PostgreSQL хранит первичные события; `check` автоматически
регистрирует прогон, документы, находки, исходные ответы, предложения и отказы.
Работающий worker забирает транзакционную очередь и строит гипотезы и векторы.
Перед следующим LLM-анализом `check` сам извлекает подтверждённые, отклонённые и
спорные случаи. Отдельных действий по раскладыванию находок от агента не требуется.
`show-review` и `verify-review` ничего в память не добавляют.

`check`/`review` сохраняют схему 1.0. Квитанция памяти и основания recall идут
отдельно в stderr и в журнал БД. Основной промпт LLM имеет версию
`ru-technical-review/3`; консолидация использует независимый
`language-memory-consolidation/2` и собственную JSON Schema.

## Запуск в Windows

Python остаётся нативным Windows-процессом. Нужны Docker Desktop с Linux containers,
Python/uv из манифестов и явно настроенная локальная LLM. Docker используется для БД.
Базовая установка и `--no-memory` работают без PostgreSQL и optional extra.

```powershell
uv sync --locked --extra memory
# Сохраните пароль в своём менеджере секретов, а UUID — в пользовательском конфиге.
$env:LANGUAGE_REVIEW_DB_PASSWORD = [guid]::NewGuid().ToString('N')
$env:LANGUAGE_REVIEW_MEMORY_DSN = "postgresql://language_review:$($env:LANGUAGE_REVIEW_DB_PASSWORD)@127.0.0.1:54329/language_review"
.\scripts\memory-db.ps1 -Action Up
uv run --locked --extra memory language-review memory migrate
uv run --locked --extra memory language-review memory health
```

Compose закрепляет образ по digest: PostgreSQL 18.6, pgvector 0.8.7.
Именованный volume монтируется в `/var/lib/postgresql` согласно layout PostgreSQL 18.
TCP опубликован только на `127.0.0.1`. Клиент принимает только loopback DSN без
service/options; DSN читается из переменной окружения и не выводится в диагностику.
Credentials БД дают операторский доступ ко всей этой БД. UUID и SQL-фильтры разделяют
контекст приложения, но не заменяют отдельные БД/credentials для недоверенных операторов.

Внешний пользовательский TOML (не в исследуемом репозитории):

```toml
[check]
engines = ["llm"]
[check.llm]
provider = "ollama"
timeout_seconds = 120
[check.llm.ollama]
model = "qwen3.5:4b"

[memory]
enabled = true
dsn_env = "LANGUAGE_REVIEW_MEMORY_DSN"
recall_mode = "hybrid"
embedding_model = "embeddinggemma:300m"
embedding_revision = "85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1"
embedding_dimensions = 768

[memory.projects]
root-0001 = "739f46de-84f9-4a91-9987-c9353092a8fb"
```

UUID в примере синтетический: создайте свой один раз и сохраняйте его при переносе
проекта. Идентичность не вычисляется из пути, fixture name или имени папки.
Нескольким корням нужны явные bindings; файл и каталог — разные границы корня.
Изменение границы или переименование файла создаёт новый случай; автоматического
небезопасного слияния по цитате нет. Группы родственных случаев производные.

Embedding-модель установите через локальный Ollama API; после загрузки проверьте digest
из `/api/tags`, размерность и прогоните `scripts/benchmark-memory.py`. Пример digest
выше относится к испытанной модели, не к любому будущему содержимому того же tag.
При несовпадении digest embedding отказывается работать, а recall явно показывает
переход к структурному поиску. Другая модель молча не выбирается.

```powershell
Invoke-RestMethod http://127.0.0.1:11434/api/pull -Method Post -ContentType application/json `
    -Body '{"model":"embeddinggemma:300m","stream":false}' -TimeoutSec 600
(Invoke-RestMethod http://127.0.0.1:11434/api/tags).models | Select-Object name,digest
uv run --locked --extra memory language-review --config $config check $source --view review --format json
# Для одного корня UUID можно передать через --memory --memory-project UUID.
```

Фоновый обработчик запускается один раз как часть эксплуатации приложения:

```powershell
uv run --locked --extra memory language-review --config $config memory work --project $projectId --watch
# Либо отдельный скрытый Windows-процесс; вернётся PID для контролируемой остановки.
.venv\Scripts\Activate.ps1
$workerProcessId = .\scripts\start-memory-worker.ps1 `
    -Config $config -Project $projectId -LogDirectory $externalLogDirectory
# Остановка собственного worker вместе с Python launcher-процессами:
taskkill /PID $workerProcessId /T /F
```

Без работающего worker первичные события всё равно устойчиво записываются, очередь
видна через `memory status`; подтверждённые структурные случаи доступны сразу.
`--watch` не является Windows service: после завершения процесса его нужно запускать
снова (при постоянной эксплуатации — через настроенный оператором Task Scheduler).

## Решение как штатный вход продукта

`accepted` подтверждает диагноз, `rejected` его отклоняет, `disputed` сохраняет спор.
Уверенность LLM, повторные замечания и изменение файла не создают таких решений.
Интерфейс — `decide-review` или Python `memory.cli.submit_decision`; оба используют
[упакованный контракт](../src/language_review/memory/decision.schema.json).

```json
{
  "schema": "language-review.decision", "schema_version": "1.0",
  "project_id": "739f46de-84f9-4a91-9987-c9353092a8fb",
  "run_id": "e0f674ca-0288-4211-b23e-b2547f11b11f", "item_id": "ID-из-review",
  "decision": {
    "id": "87ede2d0-fcb0-47a2-a121-827283b8559f",
    "expected_revision": 0, "status": "accepted",
    "actor": "reviewer", "reason": "Проверено по указанному языковому основанию.",
    "suggestion": null, "scope": "project", "public_rule": null
  }
}
```

```powershell
uv run --locked --extra memory language-review --config $config decide-review $decisionJson
uv run --locked --extra memory language-review --config $config memory run --project $projectId $runId
uv run --locked --extra memory language-review --config $config memory finding --project $projectId $findingId
```

Содержание и автор решения задаются явно; продукт не удостоверяет личность автора.
`suggestion: null` не подтверждает ни одной правки. Нулевой индекс `suggestion`
подтверждает только соответствующий `candidate` из конкретного исходного прогона.
Повтор того же `decision.id` идемпотентен; другое содержание с тем же ID отклоняется.
Конкурентные решения требуют актуальную `expected_revision`. История не стирается.

`scope: global` разрешён только для accepted operational-случая с явно написанным
`public_rule`. В другие проекты уходит только этот опубликованный текст и непрозрачные
ссылки, без исходной цитаты, пути и истории частного проекта. Ни worker, ни число
подтверждений не повышают проектное исключение до глобального.

## Запись, очередь и поиск

- До анализа регистрируется `started`; primary events, документы, occurrences и jobs
  финального результата записываются одной транзакцией. Прерванный прогон остаётся
  диагностируемым. При сбое PostgreSQL команда завершается кодом **6**, не выдаёт
  успешный JSON и не заявляет о сохранении. Это строгая политика, локального spool нет.
- ID случая учитывает UUID проекта, относительный файл, SHA-256 исходных байтов,
  диапазон, категорию, стабильные идентификаторы свидетельств и версии движков/политики.
  Wording объяснения и confidence не создают новый случай. Каждое наблюдение и варианты
  сохраняются в occurrence/event конкретного прогона; они не являются новыми решениями.
- `--memory-run UUID` предназначен для повторной доставки одного результата.
  Изменившийся результат с тем же UUID отклоняется. Обычно каждый запуск получает новый UUID.
- Jobs захватываются через `FOR UPDATE SKIP LOCKED`; lease и UUID владельца отсекают
  запоздалый worker. Число попыток ограничено, backoff ограничен; после падения lease
  освобождается через пять минут. Завершённые ревизии не перерабатываются при каждом check.
- Worker читает текущий случай и до 16 родственных случаев той же категории/области.
  Структурный ответ проверяется, включая реальные ссылки и ревизии. Любое LLM-правило
  остаётся `hypothesis`. Решение инвалидирует зависимые обобщения до следующей LLM-обработки.
- Ноль правил, повреждённый ответ, поддельные ссылки, timeout или недоступная модель
  сохраняют primary events и дают наблюдаемую retry/failed-очередь.
- Поиск объединяет русскую/точную FTS через GIN, технические токены и cosine pgvector
  через RRF. Векторный поиск точный; HNSW не создаётся, масштабная нагрузка не измерена.
  Embedding space хранит model/digest/dimensions/версию преобразования; переиндексация
  меняет только производные данные. Каждая запись проверяется distance read-back.
- Automatic recall ограничен размером, типом контекста, областью и достоверностью;
  результат всегда advisory. Он не редактирует источники, не подменяет excerpt и не
  обходит existing protected-range/negation/coordinate checks. Контекст передаётся как
  недоверенные данные. Машинные секреты/локальные пути удаляются целыми скалярами до
  сохранения и embedding; произвольный секрет без распознаваемого признака не распознаётся.

```powershell
uv run --locked --extra memory language-review --config $config memory status --project $projectId
uv run --locked --extra memory language-review --config $config memory search --project $projectId 'HTTP timeout'
uv run --locked --extra memory language-review --config $config memory reindex --project $projectId --limit 256
uv run --locked --extra memory language-review --config $config memory retry --project $projectId
```

`reindex` перестраивает один bounded batch; worker достраивает остаток. Сверяйте
`status.index.rules/indexed`, ошибки и фактическую выдачу поиска. `--include-hypotheses`
доступен для диагностики `search`; автоматический check не использует LLM-гипотезы как нормы.

Исторический импорт требует schema/link/source verification существующего `verify-review`,
явных root/project bindings и dataset. Он не подтверждает язык и идемпотентен:

```powershell
uv run --locked --extra memory language-review --config $config memory import $reviewJson `
    --root "root-0001=$source" --project "root-0001=$projectId" --dataset operational
```

## Обновление, backup и восстановление

```powershell
.\scripts\memory-db.ps1 -Action Backup -BackupPath $newExternalBackup
.\scripts\memory-db.ps1 -Action Stop
```

Stop и обычный `docker compose -f compose.memory.yml down` сохраняют volume.
Не используйте `down --volumes` для рабочей памяти. Перед обновлением остановите worker,
сделайте backup, проверьте его восстановление в отдельном Compose project и только затем
меняйте pinned image/код. Для major PostgreSQL нужен dump/restore в новый volume,
а не запуск нового major на старом каталоге. После обновления запускайте `memory migrate`
и `memory health`: миграции сериализованы advisory lock и проверяют checksum; неизвестный
или изменённый migration history отклоняется. Автоматического downgrade нет.

`memory-db.ps1 -Action Restore -BackupPath ... -Project ...` принимает только пустую
целевую БД. Используйте новый проект/порт и прежний пароль из внешнего окружения.
Восстановление выполняется `pg_restore --single-transaction --exit-on-error`; затем
проверьте health, row/digest integrity, FTS и векторный поиск. Бинарный dump передаётся
через `docker cp`, не через текстовый PowerShell pipeline.

## Приёмка и независимая оценка

```powershell
.\scripts\verify-memory.ps1 -LiveLlmModel 'qwen3.5:4b' `
    -EmbeddingModel 'embeddinggemma:300m' `
    -EmbeddingRevision '85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1'
uv run --locked --extra memory python scripts/benchmark-memory.py `
    --model embeddinggemma:300m --model qwen3-embedding:0.6b --output $newExternalJson
```

Приёмка создаёт уникальный тестовый Compose project, делает реальный backup → потерю
volume → restore, сверяет все таблицы и pgvector. Удаляется только сгенерированное
тестовое хранилище; backup и контрольные суммы остаются вне checkout. Отсутствие
параметров live LLM/embedding означает NOT_RUN этих проверок.

`language-review-eval run` передаёт split в product capture. Test и validation записываются
в карантин `test`, не ставятся в LLM/embedding-очередь и не принимают решения. Dev-записи
не используются для recall следующих повторов. Обычный retrieval читает operational/train;
`knowledge_dataset="synthetic"` явно выбирает отдельный синтетический опыт. Не заполняйте
память из gold-файлов или test/holdout. Для сравнения используйте неизменённый корпус и
три конфигурации: memory disabled, structured, hybrid; результаты сравнивает existing
`language-review-eval compare`. Синтетические метрики retrieval и ingestion не доказывают
рост языковых Precision/Recall. Независимый реальный русский baseline всё ещё необходим.

Выбор backend и реальные границы доказательств: [memory-evidence.md](memory-evidence.md).
