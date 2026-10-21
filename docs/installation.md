# Установка Windows и глобальная интеграция

Требуются Windows x64 и PowerShell 7 (`pwsh`). Для Windows PowerShell 5 установите
PowerShell 7, например отдельной пользовательской командой
`winget install Microsoft.PowerShell`, затем откройте `pwsh`.
Права администратора для Language Review не нужны. ARM64 пока не проверен.

## Установка одной командой

Команда ниже обращается к официальному **опубликованному stable** GitHub Release.
Пока первый релиз не опубликован, API вернёт ошибку; это не обещание существующего
asset или назначение номера первого релиза. Не заменяйте адрес на `main`.

```powershell
& { $r = Invoke-RestMethod 'https://api.github.com/repos/AliceLiddell01/language-review/releases/latest' -MaximumRedirection 0; $a = @($r.assets | Where-Object name -EQ 'install.ps1'); if ($r.draft -or $r.prerelease -or $a.Count -ne 1 -or $a[0].digest -notmatch '^sha256:[a-f0-9]{64}$' -or $a[0].browser_download_url -ne "https://github.com/AliceLiddell01/language-review/releases/download/$($r.tag_name)/install.ps1") { throw 'Нет проверяемого установщика stable' }; $p = Join-Path ([IO.Path]::GetTempPath()) ('language-review-' + [guid]::NewGuid().ToString('N') + '.ps1'); try { Invoke-WebRequest $a[0].browser_download_url -OutFile $p; if ((Get-FileHash -LiteralPath $p).Hash.ToLowerInvariant() -ne $a[0].digest.Substring(7)) { throw 'SHA-256 не совпадает' }; & $p -Tag $r.tag_name } finally { if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p } } }
```

Для просмотра перед выполнением сохраните `install.ps1` выбранного релиза из раздела
Assets, сравните `Get-FileHash` с `digest` этого asset в GitHub API, прочитайте файл
в редакторе и запустите `pwsh -NoProfile -File .\install.ps1 -Tag $Tag`.
Для конкретной версии/prerelease замените `/releases/latest` в загрузке metadata на
`/releases/tags/$Tag` и проверяйте тот же tag при запуске. Не используйте скрипт,
полученный из другого релиза, для первого bootstrap.

Модель доверия: TLS/GitHub API официального репозитория и права издателя. GitHub
asset digest и hashes manifest проверяют integrity в этой границе. Они **не являются
независимой цифровой подписью**, не защищают от компрометации аккаунта издателя и не
обещают проверенную Sigstore attestation. Если организация требует независимую
authenticity, до запуска подтвердите digest через собственный доверенный канал.
Установщик проверяет exact URL/tag/SHA/имя/размер/hash, запрещает redirects за пределы
GitHub hosts, не выполняет команды из metadata и не извлекает произвольные zip paths.
Прерванные загрузки не переключают рабочую установку.

Если uv отсутствует, bootstrap скачивает конкретный uv 0.13.1 для Windows x64,
проверяет digest официального GitHub asset и извлекает только `uv.exe`. Затем uv
получает Python 3.15 (официальная модель доверия managed Python uv). Python и uv не
надо устанавливать заранее. Уже установленный uv должен быть >=0.13.1. CLI получает
собственный venv, runtime dependencies устанавливаются из hash-locked requirements
только бинарными wheel. Системный Python и чужие uv tools не изменяются.
Работа uv с Python описана в [официальной документации](https://docs.astral.sh/uv/concepts/python-versions/).

`-Extras core` — базовый CLI; `-Extras chatgpt`, `memory`, `all` включают Python
зависимости добровольных возможностей. Это не включает согласия на передачу текстов,
не устанавливает Docker, серверы, Vale или LLM-модели. `-Codex` и `-Claude` явно
выбирают глобальные skills. `-NonInteractive` исключает вопросы; без него в конце
предлагается интерактивный setup. `-NoPath` отключает регистрацию PATH.
Файлы приложения находятся в `%LOCALAPPDATA%/LanguageReview/application`,
OAuth DPAPI остаётся в соседнем `LanguageReview/chatgpt`; существующая сессия
не мешает установке и не входит в область удаления приложения.

После установки откройте **новый терминал** из любого каталога:

```powershell
language-review --version
language-review-eval --help
language-review installation status
language-review doctor --format json
language-review config show --environment --format json
language-review setup
```

Ядро установлено не означает готовности всех движков. Doctor показывает по отдельности
доступность сервисов и программ; отсутствие выбранного optional движка даёт issues,
а не повод молча выбрать другой. Конфликт другого `language-review` в PATH блокирует
установку с объяснением. Изменённые пользовательские skills блокируют замену.

## Setup и проект

`setup --non-interactive --codex --no-claude` выбирает skills без вопросов;
повторный setup сохраняет неуказанный выбор. Для окружения доступны
`--ollama-endpoint`, `--llama-cpp-endpoint`, `--languagetool-endpoint` с локальными HTTP
адресами. Setup редактирует только собственный помеченный блок пользовательского TOML.
Уже существующий ручной TOML он сохраняет и предлагает изменить вручную.
Глобальные модели, prompts, effort и языковая политика не создаются.

`setup --chatgpt-login` явно запускает существующий OAuth. Для отправки текстов
по-прежнему нужно отдельное cloud consent, для recall в облако — отдельное
cloud-memory consent. Ни сессия OAuth, ни установка, ни skill, ни YAML их не выдают.
Токены остаются в существующем DPAPI-хранилище пользователя.

Установка/setup/update/doctor никогда не создают `.language-review.yaml`.
Для нового репозитория отдельной явной командой:

```powershell
language-review init $Repository
language-review config validate $Repository
language-review config show $Repository --format json
language-review check $Repository --view review --format json --output $Report
```

`init` не перезаписывает существующий YAML. Модель и профиль каждого репозитория
принадлежат его `.language-review.yaml`; один CLI использует разные политики.
Подробности: [проектная конфигурация](project-configuration.md).

## Глобальные skills

Setup ставит только пользовательский `language-review` в
`$HOME/.agents/skills/language-review/SKILL.md` для Codex и, при выборе,
`$HOME/.claude/skills/language-review/SKILL.md` для Claude Code. Эти пути сверены с
[Codex](https://learn.chatgpt.com/docs/build-skills) и
[Claude Code](https://code.claude.com/docs/en/skills). Это локальные пользовательские
skills; облачные сессии Claude их не читают. Перезапустите агентскую сессию.
Чужие AGENTS.md/CLAUDE.md не меняются; внутренние development skills не устанавливаются.

Skill проверяет установленный CLI и свою версию, YAML, review evidence и source hashes,
различает findings/clean/partial/unavailable. Модель выбирает продукт по проектной
политике. Применение правок требует разрешённой задачи; решения агента имеют actor
`agent:<имя/версия>` и reason, не становятся человеческим gold. Ingest уже встроен
в check. Живое обнаружение агентом и статическая проверка файла — разные доказательства.

## Обновление, откат, удаление

```powershell
language-review installation available
language-review installation available --prerelease
language-review installation update --tag $Tag
language-review installation update --tag $OlderTag --allow-downgrade
language-review installation rollback
language-review installation uninstall
```

Available только читает опубликованные релизы. Update — явная команда; doctor,
check и skill ничего не обновляют. Установщик сохраняет прежний runtime до проверки
нового CLI и согласованных skills. Отказ установки, проверки или конфликта skills
оставляет прежний active pointer. Журнал файловой транзакции восстанавливается при
следующей операции; при ручном изменении участника незавершённой транзакции операция
останавливается для разрешения конфликта. `.install-lock` предотвращает параллельные
операции; после аварийного завершения процесса перед удалением lock проверьте PID.

Uninstall удаляет только собственные неизменённые launcher/skills и свою запись PATH.
После завершения CLI отдельная скрытая PowerShell удаляет только файлы runtime с
совпавшим ownership hash; изменённые и неизвестные файлы сохраняются. Результат —
`%LOCALAPPDATA%/LanguageReview/application/uninstall-result.json`. Пользовательский TOML, OAuth,
проекты, память, backups, Docker и системные инструменты сохраняются. Служебный
receipt остаётся для безопасной переустановки; он не содержит credentials.
Сохранённый изменённый skill перед переустановкой переместите сами.

## Memory-worker без checkout

CLI и worker имеют независимые runtime. Только явный выбор memory с готовыми
Docker/Ollama/моделями запускает существующий владелец установки:

```powershell
language-review setup --non-interactive --memory-service --memory-llm-model $LocalModel --memory-embedding-model $EmbeddingModel --memory-embedding-revision $Digest --memory-embedding-dimensions $Dimensions
```

Релизный `install-memory-service.ps1 -ReleaseDirectory` использует проверенный wheel,
закреплённые requirements и тот же Scheduler/manifest/DPAPI/volume протокол, что
checkout-установка. Конкурирующая task или credentials не создаются. Для отдельной
приёмки доступны `--memory-runtime` и `--memory-project`.
Обновление CLI сохраняет прежний worker; его обновление — отдельный setup с тем же
runtime/project. Readiness и rollback task/runtime остаются у существующего владельца.
Миграции БД не откатываются автоматически: владелец проверяет старый worker после
неудачи, при несовместимости останавливается и требует восстановления backup.
Uninstall CLI оставляет worker, volume и историю. Эксплуатация/backup/удаление worker:
[предметная память](memory.md).

## Приёмка разработки

`scripts/verify-distribution.py` скачивает test release-like assets через отдельный
loopback HTTP-сервер, сверяет независимо переданный digest и выполняет настоящий
PowerShell bootstrap в изолированном профиле Windows. Исходный checkout не является
runtime. Скрипт проверяет новый PowerShell/PATH, skills, сохранность двух репозиториев,
отказы, повторную установку и удаление. Это не проверка публичной публикации и не
обнаружение skill живой моделью. Реальные release/tag/OAuth и live worker отмечаются
отдельно; локальный HTTP используется только приёмочным harness, production policy
HTTPS не ослабляется.

Для полного прогона смены версии передайте второй **синтетический** комплект через
`--upgrade-candidate`, подготовленный из того же target SHA с другой явно заданной
тестовой версией. Harness проверит реальный отказ испорченного кандидата, успешное
обновление, rollback, release sdist, findings/clean/stale hash с установленным Vale
и сохранность синтетического DPAPI payload/пользовательского TOML. Без второго
комплекта смена версии честно отмечается NOT_RUN.
Опциональный `--codex-executable $CodexExecutable` проверяет настоящий `skills/list`
Codex app-server без thread/turn и без модели. В Windows Codex использует Known Folder
для USER scope, поэтому эта проверка временно создаёт отсутствующий личный skill
и удаляет только свои байты в finally; при существующем skill отказывается его менять.
Облачные/агентские turn и live Claude при этом не запускались.
