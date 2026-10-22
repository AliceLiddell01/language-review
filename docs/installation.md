# Установка Windows и глобальная интеграция

Требуются Windows x64 и доступ к GitHub Releases. Установка не требует checkout,
заранее установленного Python, `uv` или PowerShell. Права администратора не нужны;
ARM64 пока не проверен.

## Установка одной командой

После публикации stable Release скачайте `install.exe` из официальных
[GitHub Releases](https://github.com/AliceLiddell01/language-review/releases/latest).
Пока первого stable Release нет, ссылка не сработает; не заменяйте адрес на `main`.
Запустите скачанный файл из Windows Terminal или другого терминала:

```text
"C:\Users\<имя>\Downloads\install.exe"
```

Для выбранной версии или prerelease передайте полный tag: `install.exe --tag vX.Y.Z-rc.1`.
Bootstrap сверяет официальный GitHub API, TLS, release identity, SHA-256 и размеры
assets, после чего использует только указанный комплект. `--no-path` отключает
регистрацию пользовательского PATH; `--non-interactive` выполняет только core setup;
`--extras`, `--codex` и `--claude` выбирают необязательные зависимости/skills явно.

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

`--extras core` — базовый CLI; `--extras chatgpt`, `memory`, `all` включают Python
зависимости добровольных возможностей. Это не включает согласия на передачу текстов,
не устанавливает Docker, серверы, Vale или LLM-модели. `--codex` и `--claude` явно
выбирают глобальные skills. `--non-interactive` исключает вопросы; без него в конце
предлагается интерактивный setup. `--no-path` отключает регистрацию PATH.
Повторная установка с `--codex` сохраняет ранее выбранный Claude skill и наоборот;
отсутствие положительного переключателя не отключает интеграцию. Для явного отключения
используйте `language-review setup --non-interactive --no-claude` или `--no-codex`.
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

### Переход с локальной установки до первого выпуска

Если активная установка создана прежней версией и содержит `.cmd` или PowerShell
launcher, новый bootstrap откажется менять её. Сначала проверьте новый CLI в отдельном
пустом каталоге, не добавляя его в PATH:

```text
"C:\Users\<имя>\Downloads\install.exe" --root "C:\Users\<имя>\AppData\Local\LanguageReview\migration-test" --no-path --non-interactive
"C:\Users\<имя>\AppData\Local\LanguageReview\migration-test\bin\language-review.exe" --version
```

После проверки вызовите `language-review installation uninstall` через прежний CLI и
дождитесь результата в `uninstall-result.json`. Он оставляет пользовательские TOML,
OAuth, worker, PostgreSQL volume и данные памяти. Затем снова запустите скачанный
`install.exe` без `--root`, чтобы занять стандартный каталог. Если прежний CLI не может
завершить свою установку, не удаляйте его файлы вручную: используйте отдельный пустой
root и сохраните старую установку для ручного восстановления.

Ядро установлено не означает готовности всех движков. Doctor показывает по отдельности
доступность сервисов и программ; отсутствие выбранного необязательного движка даёт issues,
а не повод молча выбрать другой. Конфликт другого `language-review` в PATH блокирует
установку с объяснением. Изменённые пользовательские skills блокируют замену.

## Setup и проект

`setup --non-interactive --codex --no-claude` выбирает skills без вопросов;
повторный setup сохраняет неуказанный выбор. Для окружения доступны
`--ollama-endpoint`, `--llama-cpp-endpoint`, `--languagetool-endpoint` с локальными HTTP
адресами. Setup редактирует только собственный помеченный блок пользовательского TOML.
Уже существующий ручной TOML он сохраняет и предлагает изменить вручную.
Если в ранее созданный блок добавлены параметры или комментарии, повторная настройка
endpoint отказывает до записи и предлагает изменить адрес вручную; пользовательские
ключи и комментарии не теряются. Неуказанные адреса и соседние ручные секции сохраняются.
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
Выбор active и очистку поколений сериализует общий для launcher и Python CLI
диапазонный Windows lock файла `.launch-lock`. После гибели владельца Windows
освобождает дескриптор; оставшееся имя файла не мешает новому захвату. Занятое имя
не удаляется: оба клиента запрещают совместное удаление открытого файла.
Работающий CLI удерживает отдельный lease своего поколения, поэтому очистка его
сохраняет. Откат использует `uv.exe` активного поколения только после проверки пути
и SHA-256 по inventory. `installation status` отдельно сообщает состояние runtime,
каждого launcher и skills; диагностика не исправляет изменённые файлы.

Uninstall удаляет только собственные неизменённые launcher/skills и свою запись PATH.
После завершения CLI отдельный Python helper удаляет только файлы runtime с
совпавшим ownership hash; изменённые и неизвестные файлы сохраняются. Результат —
`%LOCALAPPDATA%/LanguageReview/application/uninstall-result.json`. Пользовательский TOML, OAuth,
проекты, память, backups, Docker и системные инструменты сохраняются. Служебный
receipt остаётся для безопасной переустановки; он не содержит учётных данных.
Сохранённый изменённый skill перед переустановкой переместите сами.

## Memory-worker без checkout

CLI и worker имеют независимые runtime. Только явный выбор memory с готовыми
Docker/Ollama/моделями запускает существующий владелец установки:

```powershell
language-review setup --non-interactive --memory-service --memory-llm-model $LocalModel --memory-embedding-model $EmbeddingModel --memory-embedding-revision $Digest --memory-embedding-dimensions $Dimensions
```

Релизный `language-review setup --memory-service` использует wheel из проверенного
release directory и тот же Scheduler/manifest/DPAPI/volume протокол, что установка из
checkout. Конкурирующая задача или учётные данные не создаются. Для отдельной приёмки
доступны `--memory-runtime` и `--memory-project`.
Обновление CLI сохраняет прежний worker; его обновление — отдельный setup с тем же
runtime/project. Readiness и rollback task/runtime остаются у существующего владельца.
Миграции БД не откатываются автоматически: владелец проверяет старый worker после
неудачи, при несовместимости останавливается и требует восстановления backup.
Uninstall CLI оставляет worker, volume и историю. Эксплуатация/backup/удаление worker:
[предметная память](memory.md).

## Приёмка разработки

`scripts/verify-distribution.py` скачивает test release-like assets через отдельный
loopback HTTP-сервер, сверяет независимо переданный digest и выполняет настоящий
frozen Python bootstrap в изолированном профиле Windows без Python, `uv` и shell в PATH.
Исходный checkout не является runtime. Скрипт проверяет установленные CLI, PATH, skills, сохранность двух репозиториев,
отказы, повторную установку и удаление. Это не проверка публичной публикации и не
обнаружение skill живой моделью. Реальные release/tag/OAuth и live worker отмечаются
отдельно; локальный HTTP используется только приёмочным harness, production policy
HTTPS не ослабляется.
PATH регистрирует сам установщик. Независимый дочерний Python-процесс читает постоянные
Machine/User PATH и проверяет путь обоих найденных launcher из другого каталога;
после удаления тестовой записи этот же сценарий должен отказать. В `finally`
приёмка удаляет только собственную уникальную запись, сохраняя остальные значения PATH.

Для полного прогона смены версии передайте второй **синтетический** комплект через
`--upgrade-candidate`, подготовленный из того же target SHA с другой явно заданной
тестовой версией. Harness проверит реальный отказ испорченного кандидата, успешное
обновление, rollback, release sdist, findings/clean/stale hash с установленным Vale
и сохранность синтетического DPAPI payload/пользовательского TOML. Без второго
комплекта смена версии честно отмечается NOT_RUN.
Для релизного доказательства используйте `scripts/release.py accept`:
он получает настоящий предыдущий стабильный выпуск и передаёт его как
`--previous-candidate`. Локальный `--upgrade-candidate` остаётся средством разработки;
его синтетический комплект не заменяет предыдущий опубликованный релиз.
Опциональный `--codex-executable $CodexExecutable` проверяет настоящий `skills/list`
Codex app-server без thread/turn и без модели. В Windows Codex использует Known Folder
для USER scope, поэтому эта проверка временно создаёт отсутствующий личный skill
и удаляет только свои байты в finally; при существующем skill отказывается его менять.
Облачные/агентские turn и live Claude при этом не запускались.
