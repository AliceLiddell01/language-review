# Language Review

Установка Windows, глобальные пользовательские skills и управление установленным CLI:
[руководство установки](docs/installation.md). Подготовка кандидата и отдельно
разрешаемый GitHub Release: [политика выпуска](docs/releases.md).

[![Проверки проекта](https://github.com/AliceLiddell01/language-review/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/AliceLiddell01/language-review/actions/workflows/ci.yml)

**Локальная CLI-утилита для языкового ревью русского технического текста в исходном коде и документации.**

Language Review находит текст в комментариях, сообщениях и документах, проверяет его с помощью **LanguageTool, Vale и LLM** и привязывает замечания к исходным файлам. Результат можно читать в терминале, сохранять в JSON или передавать агенту-разработчику для самостоятельной проверки.

> [!IMPORTANT]
> Инструмент **не исправляет исходники автоматически**. Каждое замечание — гипотеза, а не готовая безопасная правка. Независимая оценка точности на реальном русском техническом корпусе пока не выполнена.

[Быстрый старт](#быстрый-старт) · [Проверка языка](#проверка-языка) · [Ревью для агента](#ревью-для-агента) · [Форматы](#поддерживаемые-форматы) · [Документация](#документация)

## Как это работает

| Команда | Результат | Нужны языковые движки? |
| --- | --- | --- |
| `scan` | Инвентаризация файлов: тип, кодировка, BOM, переводы строк, размер и состояние чтения | Нет |
| `extract` | Извлечение человекочитаемых фрагментов с точными диапазонами в исходнике | Нет |
| `check` | Проверка извлечённых фрагментов выбранными движками, замечания и покрытие | Да |
| `show-review` | Чтение ранее сохранённого ревью без повторной проверки | Нет |
| `verify-review` | Сверка сохранённого ревью с текущими байтами, цитатами и координатами файлов | Нет |

`scan`, `extract` и `check` запускаются независимо; `check` выполняет извлечение самостоятельно. Для агента доступно представление `check --view review` с приоритетами, свидетельствами разных движков и причинами отклонения предложений. [Подробнее о контракте ревью](docs/agent-review.md).

## Быстрый старт

**Среда разработки и приёмки:** Windows / PowerShell. Требуются Git, **Python ≥ 3.15** и **uv ≥ 0.13.1**; репозиторий фиксирует Python 3.15.0 в [`.python-version`](.python-version), зависимости — в `uv.lock`. При сборке `tree-sitter` из исходников с этой версией Python на Windows потребуются Visual Studio Build Tools с MSVC и компонентами C++.

### Установка и первый запуск

```powershell
git clone https://github.com/AliceLiddell01/language-review.git
Set-Location language-review
uv sync --locked

uv run --locked language-review --version
uv run --locked language-review scan .\README.md
uv run --locked language-review extract .\README.md
```

Эти команды не требуют LanguageTool, Vale, LLM, OAuth или Docker. В самом клоне уже присутствует `.language-review.yaml`.

### Анализ другого репозитория

Замените `..\repo` на путь к существующему каталогу целевого репозитория:

```powershell
uv run --locked language-review init ..\repo
uv run --locked language-review config validate ..\repo
uv run --locked language-review scan ..\repo
uv run --locked language-review extract ..\repo
```

`init` создаёт `.language-review.yaml` и **не перезаписывает** существующую конфигурацию. Если она уже есть, пропустите `init`. Без проектного YAML команды `scan`, `extract` и `check` завершаются ошибкой. Правила определения корня Git/worktree и работы с каталогами вне Git приведены в [руководстве по конфигурации](docs/project-configuration.md#корень-и-пути).

Все следующие команды выполняются из клона Language Review с `uv run --locked`. Справка: `uv run --locked language-review --help`.

## Проверка языка

Для `check` сначала подключите нужный движок. `uv sync` устанавливает Python-зависимости, **но не сами внешние языковые сервисы**.

| Движок | Для чего используется | Что требуется |
| --- | --- | --- |
| **LanguageTool** | Орфография, грамматика, пунктуация, типографика | Локальный HTTP-сервер |
| **Vale** | Стиль, терминология, правила для русского текста | Установленная программа Vale; русские правила входят в пакет |
| **LLM** | Контекстные языковые гипотезы, стиль и остаточный английский | `llama-server` или Ollama; ChatGPT подключается отдельно, с согласием |

Инструкции по установке, моделям и диагностике — в [руководстве по движкам](docs/language-engines.md).

```powershell
# Проверка локальными LanguageTool и Vale
uv run --locked language-review check ..\repo --engine languagetool --engine vale

# LLM через настроенный Ollama
uv run --locked language-review check ..\repo --engine llm --llm-provider ollama --category spelling --category grammar

# Машинный отчёт (вне проверяемого репозитория)
uv run --locked language-review check ..\repo --engine vale --format json --output ..\check-report.json
```

Без `--engine` используется профиль проекта; шаблон `init` включает все три движка. Провайдер LLM по умолчанию — `llama_cpp`. Если выбранный движок или модель недоступны, Language Review **не заменяет их молча**. Предложения проходят проверки координат и защищённого содержимого; даже статус `candidate` требует ручного анализа.

### Отчёты и фильтрация

```powershell
uv run --locked language-review scan ..\repo --format json --output ..\scan-report.json
uv run --locked language-review extract ..\repo --format json --output ..\extract-report.json
```

Без `--output` вывод идёт в stdout; диагностика JSON-режима — в stderr. Все три основные команды учитывают `.gitignore`, профили, исключения и ограничения размера. Дополнительные параметры: `--exclude`, `--extension`, `--only-extension` и `--follow-symlinks`. Обход символических ссылок по умолчанию выключен; при включении ограничен корнем и защищён от циклов. [Настройки выбора файлов](docs/project-configuration.md).

## Ревью для агента

```powershell
uv run --locked language-review check ..\repo --engine vale --view review --format json --output ..\review.json
uv run --locked language-review show-review ..\review.json --all
uv run --locked language-review verify-review ..\review.json --root 'root-0001=..\repo' --format json
```

`--view review` не запускает дополнительные движки: он представляет результат того же `check` с отдельными свидетельствами, рисками, приоритетами и покрытием. `show-review` открывает сохранённый JSON без анализа. В `verify-review` сопоставьте `root-0001` с ID корня **из своего отчёта**; проверяются SHA-256, исходные цитаты и координаты.

Статус `current` означает, что локализация согласована с указанными файлами. Он **не доказывает правильность диагноза, безопасность замены или подлинность отчёта**. [Формат и ограничения ревью](docs/agent-review.md).

## Поддерживаемые форматы

| Форматы | Извлекаемый текст |
| --- | --- |
| Rust (`.rs`), C# (`.cs`), C/C++ (`.c`, `.cpp`, `.cc`, `.cxx`, `.h`, `.hpp`, `.hh`, `.hxx`) | Комментарии, документационные комментарии, строки известных или настроенных вызовов сообщений |
| PowerShell (`.ps1`, `.psm1`) | Комментарии, справка, сообщения и here-strings |
| Markdown (`.md`, `.markdown`) | Заголовки, абзацы, списки, GFM-таблицы и подписи ссылок |
| TXT, reStructuredText, AsciiDoc (`.txt`, `.rst`, `.adoc`) | Непустые строки TXT и поддерживаемые элементы разметки |
| JSON, YAML, TOML, XML (`.json`, `.yaml`, `.yml`, `.toml`, `.xml`) | Только строковые значения по явно заданным селекторам; расширения включаются отдельно |

Первые четыре группы расширений включены по умолчанию. Структурированные файлы требуют настройки `extract.selectors` в `.language-review.yaml`. Markdown-код и цитаты не анализируются как проза; неоднозначный или повреждённый синтаксис может давать `partial`. Для `.h` язык можно задать через `extract.header_language`. Селекторы, ограничения парсеров и `extract.message_calls` подробно описаны в [конфигурации](docs/project-configuration.md).

## Конфигурация и дополнительные возможности

**Проектная политика:** `.language-review.yaml` в корне проверяемого репозитория задаёт профили, движки, категории, термины, селекторы, стратегии и бюджеты анализа.

**Пользовательская среда:** `%APPDATA%\language-review\config.toml` (или `--config TOML`) задаёт адреса сервисов, исполняемый Vale, OAuth, память и разрешения на передачу данных. Проектный YAML сам по себе не даёт сетевых полномочий.

```powershell
uv run --locked language-review config show ..\repo --format json
uv run --locked language-review config show --environment --format json
uv run --locked language-review doctor --format json
```

Доступны дополнительные, отдельно документированные сценарии:

- **[Многоступенчатый анализ](docs/multistage-analysis.md):** режимы `normal`, `adaptive` и `deep`, стратегии и отдельная проверка гипотез; Sign in with ChatGPT с отдельными согласиями на облако и передачу памяти.
- **[Предметная память](docs/memory.md):** опциональная PostgreSQL/pgvector через Docker Compose, журнал проверок, решения `decide-review`, консолидация и поиск; необходим extra `memory`.
- **[Независимая оценка](docs/evaluation.md):** CLI `language-review-eval` для корпусов, сохранённых отчётов, разметки, покрытия и сравнения результатов. Синтетические контрактные тесты не заменяют оценку на реальных репозиториях.

## Границы и безопасность

- **Исходники не изменяются:** `scan`, `extract`, `check`, `show-review`, `verify-review`, `doctor` и `config show` работают без правок целевых файлов. `init` создаёт конфигурацию, `--output` записывает отчёт, включённая память отдельно пишет в БД.
- **Нет автоматического применения исправлений, SARIF и проверки Git diff.** Это не возможности текущей версии.
- **Облако только с разрешениями:** по умолчанию используется локальная обработка; нелокальные endpoints требуют `check.allow_remote_endpoints = true`, а ChatGPT — отдельных согласий. OAuth-вход сам по себе их не даёт.
- **Отчёты могут содержать исходный текст.** Не публикуйте результаты с секретами или пользовательскими данными. LLM способна пропускать ошибки и предлагать смысловые искажения.

Результаты различают полную проверку (`clean`, `findings`), неполное покрытие (`partial`), недоступность движков (`unavailable`) и ошибки. Для `check` код `0` — полная проверка без замечаний, `1` — полная проверка с замечаниями, `4` — неполная проверка, `5` — недоступность движка. Другие коды и версии JSON-схем указаны в [документации](#документация).

## Документация

| Раздел | Содержание |
| --- | --- |
| [Конфигурация проекта](docs/project-configuration.md) | Корни, профили, пути, селекторы, промпты, перенос политики из TOML |
| [Языковые движки](docs/language-engines.md) | LanguageTool, Vale, llama.cpp/Ollama, настройка и реальные приёмочные проверки |
| [Ревью для агента](docs/agent-review.md) | Приоритеты, свидетельства, отчёты и проверка актуальности |
| [Многоступенчатый анализ](docs/multistage-analysis.md) | Стратегии, бюджеты, проверка гипотез, ChatGPT |
| [Предметная память](docs/memory.md) | PostgreSQL/pgvector, конфигурация, эксплуатация и восстановление |
| [Независимая оценка](docs/evaluation.md) | Корпус, разметка, метрики и ограничения доказательств |
| [Процесс разработки](docs/development-workflow.md) | Ветки, ревью, CI и безопасность |

**Машинные контракты и примеры:** [`extract`](docs/extract-report.schema.json) ([пример](docs/extract-report.example.json)), [`check`](docs/check-report.schema.json) ([пример](docs/check-report.example.json)), [`review`](docs/review-report.schema.json) ([пример](docs/review-report.example.json)), [расширенный review](docs/multistage-review.example.json), [`verify-review`](docs/review-verification.schema.json). Схемы `scan` и `extract` имеют версию 1.0; `check` и `review` поддерживают 1.0/1.1 в зависимости от режима.

Для разработки используйте команды из [контекста проверок](.codex/context/verification.md). `scripts/verify-package.ps1` проверяет сборку и установленный CLI; контрактные тесты без реальных языковых сервисов не измеряют качество анализа русского текста.
