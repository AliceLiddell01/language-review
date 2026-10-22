# Версии и контролируемый выпуск

Merge/commit в `main` не является релизом. Обычный CI не создаёт тегов, не меняет
версию и не публикует assets. `pyproject.toml` содержит метаданные разработки;
их наличие не доказывает существование выпуска. Источник опубликованных версий —
GitHub Releases `AliceLiddell01/language-review` и соответствующие теги.
Первый публичный номер этим изменением не назначается.

## Решение о версии

Только после отдельного запроса на выпуск разработчик/агент изучает diff между
предыдущим подтверждённым stable и выбранным полным SHA, фактические изменения CLI,
проектной конфигурации, отчётов, skills, runtime и миграций. В release notes он
указывает конкретную выбранную версию и причину: совместимая возможность, исправление
или несовместимый контракт. Количество commits, название PR и Conventional Commit
не назначают версию автоматически.

Используется [SemVer](https://semver.org/spec/v2.0.0.html): для `1.y.z` несовместимый
публичный контракт требует major, совместимая возможность — minor, исправление — patch.
`0.y.z` означает начальную разработку с нестабильным API: команда явно описывает
совместимость и обычно группирует несовместимые изменения в minor. Значимость функции
сама по себе не требует `1.0.0`; переход к стабильному API — отдельное решение.
Разрешены `X.Y.Z-alpha.N`, `X.Y.Z-beta.N`, `X.Y.Z-rc.N`, N начинается с 1.
PEP 440 в wheel отображает их как `aN`, `bN`, `rcN`; mapping однозначен.
Build metadata и произвольные prerelease не поддерживаются.

Последний stable и последний prerelease различаются. Установщик по умолчанию
использует `/releases/latest` (stable), prerelease требует явного tag либо флага
`installation available/update --prerelease`. Нельзя перезаписывать существующие
версии, теги или assets. Понижение установленного CLI требует явного разрешения
`--allow-downgrade` либо `installation rollback`.

Версия продукта не является `schema_version` YAML или отчёта. Проектный YAML
сохраняет `schema_version: 1`, check/review сохраняют существующие версии 1.0/1.1;
prompt revision меняется только при изменении собственного контракта.

## Кандидат без публичного тега

Вызов только по отдельному запросу на подготовку релиза, после выбора переменных:

```powershell
uv run --locked python scripts/release.py prepare --version $Version --channel $Channel --commit $Commit --previous $PreviousStable --notes $NotesFile --output $CandidateDirectory
uv run --locked python scripts/release.py accept --candidate $CandidateDirectory --output $AcceptanceFile
```

`$Commit` — полный SHA, `$PreviousStable` — последний опубликованный стабильный tag
либо `none`. Подготовка читает реальную историю GitHub и проверяет отсутствие тега.
Сборка извлекает `git archive` точного SHA в новый временный каталог. Только в нём
меняются package metadata и ресурс `release_identity.json`, подставляется версия
skill. `uv_build` остаётся закреплённым в pyproject; runtime зависимости экспортируются
из исходного `uv.lock` с hashes отдельно для core/chatgpt/memory/all. Lock разработки
и исходный checkout не меняются. Детерминированная идентичность не означает обещания
побайтово одинаковых gzip/zip timestamps при разных запусках сборки.

Комплект: wheel, sdist, `install.ps1`, закреплённые requirements с hashes, Windows wheelhouse,
существующие memory runtime scripts/compose, `release-notes.md`, `release.json`,
`SHA256SUMS`. Skill упакован в wheel/sdist. В manifest находятся tag/version/PEP
version/SHA/предыдущий стабильный выпуск, Python и skill contract, размеры и SHA-256 assets.
Manifest не содержит локальных путей и пользовательских данных.
Зависимости без готовых CPython 3.15 wheels собираются издателем из hash-locked
источников через pip/setuptools/wheel из `uv.lock`, без build isolation с плавающими
backend dependencies. Компилятор нужен только сборщику в Windows. Клиент устанавливает
зависимости только из проверенного `wheelhouse.zip`, с `--no-index` и hashes.

Notes должны содержать версию, SHA, предыдущий стабильный выпуск и заполненные разделы:
`## Изменения`, `## Совместимость`, `## Миграции и откат`, `## Ограничения`.
В «Совместимость» записывается обоснование версии; в миграциях — обратимость и
возможность запуска прежнего worker после миграции. Пустые разделы/TODO не принимаются.
Для notes, хранящихся в target commit, вместо его SHA используйте маркер
`@TARGET_SHA@`: `prepare` подставляет переданный полный SHA до валидации и записи
`release-notes.md` в комплект. Так содержимое commit не зависит от собственного
SHA. Уже заполненные notes с буквальным SHA также поддерживаются; публикация
проверяет итоговый текст и его digest. Пример первого кандидата:
[0.1.0-beta.1](release-notes/0.1.0-beta.1.md).

## GitHub Actions и разрешение публикации

Workflow `release.yml` запускается исключительно через `workflow_dispatch`.
`operation=prepare` имеет read-only GitHub permissions, принимает явно заданные
version/channel/SHA/previous/notes, проверяет принадлежность target к main, собирает
кандидата и выполняет Windows acceptance. Результат — Actions artifact, не Release.
Команда `accept` получает через официальный транспорт комплект `previous_stable`,
проверяет его tag/channel и выполняет установку предыдущей версии, отказ неисправного
кандидата, обновление до выпускаемого кандидата и откат с запуском CLI. Если предыдущий
комплект недоступен, подготовка останавливается. Синтетическое изменение номера версии
не заменяет эту проверку. Для первого выпуска (`previous_stable=none`) обе проверки
смены версии имеют явно разрешённый `NOT_RUN` с причиной `no_predecessor`.

Отчёт `acceptance.json` имеет схему `language-review.release-acceptance/1`.
В `checks` каждый результат содержит точный `status` (`PASS` или `NOT_RUN`),
необязательное пояснение `detail` и для пропуска — машинную причину `reason`.
Обязательные имена и допустимые причины задаёт общий модуль
`src/language_review/release_acceptance.py`; его использует и приёмка, и публикация.
Отчёт связан с tag, commit, SHA-256 manifest и предыдущим комплектом, а `source`
фиксирует репозиторий, SHA workflow, ID ручного запуска и ветку `main`.
Публикация сверяет источник с GitHub API и ID скачанного артефакта. Локальный отчёт
с `source.kind=local` для публикации не подходит.

`not_run` явно перечисляет пропуски: публичный Release/tag, агентские turn,
пользовательский OAuth и отдельную приёмку worker/БД. Допускаются также отсутствие
Vale и незапрошенное обнаружение Codex. Эти пропуски не блокируют релиз ядра.
Пропуск обязательных установки, PATH, обновления или отката при наличии предыдущего
выпуска блокирует публикацию до создания любого ref или Release; пустой `checks`
и общий `PASS` без полного набора результатов не принимаются.

После изучения notes, результатов приёмки и compatibility rationale разработчик
отдельно запускает `operation=publish` на main с ID успешного prepare run, теми же
параметрами, SHA-256 подтверждённых notes и строкой `publish vВЕРСИЯ ПОЛНЫЙ_SHA`.
Только job публикации имеет `contents: write`; он использует Environment
`release-publication`. Перед первым настоящим выпуском включите required reviewers
для этого Environment в настройках репозитория. Ручной dispatch с совпадающим
разрешением — внешняя предпосылка; manifest не может сам разрешить публикацию.

Publish повторно проверяет manifest, notes, актуальную историю релизов, отсутствие
тега, успешные CI и CodeQL именно target SHA и привязанную к manifest Windows
приёмку. Атомарно создаётся новый tag ref на полный target SHA (существующий ref
не изменяется); `gh release create --verify-tag` создаёт draft с полным комплектом,
затем все assets
скачиваются обратно и проверяются; только после этого draft публикуется. Это
совместимо с immutable releases: после публикации assets уже не добавляются.
Ошибка после создания ref/draft оставляет их для осмотра; код не удаляет и не
перезаписывает его автоматическим повтором. Существующий тег блокирует новый запуск.

Не запускайте write job для проверки разработки. Создание настоящего Release/tag
требует отдельной актуальной команды пользователя. PyPI/WinGet не задействованы.
Фактическая публичная публикация до такого разрешения — **NOT_RUN**.

Ограничения доверия и пользовательские команды: [установка](installation.md).
API: [GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository),
[ручной workflow](https://docs.github.com/en/actions/managing-workflow-runs/manually-running-a-workflow),
[gh release create](https://cli.github.com/manual/gh_release_create).
