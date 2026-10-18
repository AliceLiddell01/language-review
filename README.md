# Language Review

Language Review — локальная CLI-утилита для инвентаризации файлов и структурного извлечения человекочитаемых фрагментов из исходников и документации.

## Возможности этого этапа

Утилита перечисляет поддерживаемые файлы, определяет тип файла, кодировку с учётом BOM, переводы строк, размер и базовую читаемость. Команда `extract` сохраняет происхождение выбранных фрагментов, точные диапазоны исходных файлов, SHA-256 и защищённые машинные вставки. Команды `config show` и `doctor` показывают эффективные настройки и проверяют заданные внешние программы и локальные HTTP-сервисы.

Извлечение поддерживает комментарии и контекстные строки Rust, C#, C и C++, PowerShell (`.ps1`, `.psm1`), Markdown, обычный текст, reStructuredText и AsciiDoc. Для `.json`, `.yaml`, `.yml`, `.toml` и `.xml` нужно явно включить расширение и настроить селекторы. `.psd1` не разбирается как исполняемый код. Неоднозначный синтаксис и повреждённые файлы помечаются, а не объявляются успешными пустыми результатами.

Проверка орфографии и грамматики, вызов LLM, LanguageTool или Vale, SARIF, анализ Git diff и применение исправлений не реализованы. `extract` только читает исходники.

## Установка и запуск в PowerShell

Нужны Python 3.15 и uv 0.13.1 или новее. В репозитории выбрана последняя стабильная версия Python, доступная на момент подготовки этапа; пакет требует Python 3.15 или новее. Зависимости и версии инструментов разработки зафиксированы в `uv.lock`.

~~~powershell
uv sync --locked
uv run --locked language-review --help
uv run --locked language-review --version
uv run --locked python -m language_review --version
~~~

После `uv sync` команду можно запускать из активированного окружения:

~~~powershell
.venv\Scripts\Activate.ps1
language-review --help
~~~

## Команды

### Инвентаризация

~~~powershell
language-review scan .\src
language-review scan .\Cargo.toml .\README.md
language-review scan .\repo --format json
language-review scan .\repo --format json --output .\report.json
language-review extract .\src --format json
language-review extract .\settings.json --extension .json --format json
~~~

Формат по умолчанию — человекочитаемый. JSON содержит `schema_version`, сведения о программе и корнях, массив `files`, итоговые счётчики и статус обхода. Записи файла имеют состояния `selected`, `skipped` или `error`; языковых находок в схеме нет. При `--format json` stdout содержит только JSON, диагностика выводится в stderr.

По умолчанию учитываются `.rs`, `.cs`, `.cpp`, `.cc`, `.cxx`, `.c`, `.h`, `.hpp`, `.hh`, `.hxx`, `.ps1`, `.psm1`, `.md`, `.markdown`, `.txt`, `.rst` и `.adoc`. Новые расширения можно добавить через `--extension .toml`; `--only-extension` задаёт список вместо настроенного. Файлы с неподдерживаемым расширением, бинарным содержимым, неподходящей кодировкой или превышением лимита попадут в список пропусков с причиной.

### Извлечение текста

~~~powershell
language-review extract .\repo
language-review extract .\repo --format json
language-review extract .\repo --format json --output .\extract-report.json
language-review extract .\settings.json --extension .json --format json
~~~

`extract` использует тот же обход, фильтры, `.gitignore`, профили, лимит размера и политику ссылок, что и `scan`. Расширения `.json`, `.yaml`, `.yml`, `.toml` и `.xml` не включены в список по умолчанию: добавь их через `--extension` или настрой `scan.extensions`. Для них без селектора фрагменты не извлекаются.

Поддерживаемые форматы и границы извлечения:

| Формат | Что извлекается | Ограничения |
| --- | --- | --- |
| Rust, C#, C, C++ | Комментарии, documentation comments и строки доказуемых контекстов сообщений | `.h` с одинаково подходящей грамматикой C/C++ помечается `partial`; `header_language` задаёт выбор |
| PowerShell | Комментарии, comment-based help, строки команд сообщений и here-strings | `.psd1` отмечается `unsupported` как data-файл |
| Markdown | Заголовки, абзацы, списки, GFM-таблицы и подписи ссылок | Цитаты и code blocks исключаются; неточное сопоставление даёт `partial` |
| TXT | Непустые строки текста | Структурной разметки нет |
| reStructuredText, AsciiDoc | Заголовки и проза с базовой обработкой директив, ссылок и literal/code blocks | Поддерживается консервативное подмножество разметки |
| JSON, YAML, TOML, XML | Только строковые значения по настроенным селекторам | Расширение нужно включить явно; YAML tags/aliases не интерпретируются, повторяющиеся JSON/YAML-ключи пропускаются, TOML-массивы таблиц (`[[table]]`) помечаются `partial` |

В TOML селекторы задаются по расширению файла. Поддерживается безопасное подмножество путей: свойства через `.name`, ключи через `['name']` или `["name"]`, индексы обычного массива `[0]` и wildcard `*`. YAML flow mappings поддерживаются; повторяющиеся JSON/YAML-ключи в одном объекте/отображении пропускаются с диагностикой, потому что разные потребители могут выбрать разные значения. TOML-массивы таблиц пока пропускаются с диагностикой.

~~~toml
[scan]
extensions = [".rs", ".cs", ".md", ".json", ".yaml"]

[extract]
header_language = "auto" # auto, c или cpp для неоднозначных .h
message_calls = ["MyLogger.ShowMessage"]

[extract.selectors]
".json" = ["$.ui.message", "$.errors[*].description"]
".yaml" = ["$.ui.message"]

[profiles.docs]
priority = 5
match_paths = ["docs/**"]
[profiles.docs.extract.selectors]
".json" = ["$.ui.message"]
~~~

В отчёте извлечения схема называется `language-review.extract` и имеет собственную версию `1.0`; она не меняет JSON-схему `scan` версии `1.0`. Документ содержит относительный путь, root ID, выбранный профиль, формат, кодировку, BOM, SHA-256, статус и диагностические коды. Каждый фрагмент включает `text`/`source_text`, вид, контекст, стабильный ID, исходный диапазон и защищённые диапазоны.

`byte_offset` — смещение от начала исходных байтов, включая BOM; `line` и `column` начинаются с 1; `column` считает Unicode code points, tab считается одной колонкой, `utf16_column` считает UTF-16 code units. Конец `range` исключающий. Диапазоны фрагментов указывают на ровно те байты, которые сериализованы как исходный текст; кодированные escape-последовательности остаются в исходном виде и защищаются отдельно.

| Поле координат | Единица | Начало отсчёта |
| --- | --- | --- |
| `byte_offset` | Байты исходного файла в исходной кодировке | 0, включая BOM |
| `line` | Физические строки `LF`, `CRLF` или `CR` | 1 |
| `column` | Unicode code points; tab считается одним | 1 |
| `utf16_column` | UTF-16 code units | 1 |

Документ может иметь статус `extracted`, `skipped`, `unsupported`, `partial` или `error`. В JSON при частичных результатах stdout остаётся корректным JSON, а коды диагностики идут в stderr. `extract` возвращает `3`, если есть `partial`, `unsupported` или `error`, и `2` для ошибочного входного пути или конфигурации.

Полный реальный ответ, полученный командой `extract` на постоянной синтетической фикстуре, хранится в [extract-report.example.json](docs/extract-report.example.json). Его диапазоны, ID и SHA-256 рассчитаны из байтов фикстуры. Поля и ограничения описаны в [JSON Schema](docs/extract-report.schema.json).

Каталоги зависимостей и сборки, в том числе `.git`, `.venv`, `node_modules`, `target`, `bin`, `obj`, `build` и `dist`, исключаются по умолчанию. Для пользовательских исключений используй `--exclude 'docs/generated/**'`. Также поддерживаются `.gitignore` и отрицательные правила Git; обход ссылок выключен по умолчанию. `--follow-symlinks` включает ссылки только в пределах явно указанного корня и защищает от циклов.

Для стандартной конфигурации выбирай проектный профиль по маркерам, заданным в пользовательском конфиге; `--profile NAME` выбирает профиль явно. Профили с `match_paths` могут задавать настройки для относительных путей внутри корня сканирования.

### Диагностика

~~~powershell
language-review doctor
language-review doctor .\src
language-review doctor --format json
~~~

`doctor` проверяет установленный пакет, версию Python, загруженную конфигурацию, заданные пути, программы из `doctor.programs` и доступность HTTP-сервисов на локальном компьютере из `doctor.http_services`. Недоступность необязательной программы не мешает `scan`, но отражается в результате диагностики и коде завершения.

### Конфигурация

~~~powershell
language-review config show
language-review config show --format json
language-review --config .\language-review.toml scan .\repo
~~~

Пользовательский конфиг хранится в `%APPDATA%\language-review\config.toml` на Windows и в `$XDG_CONFIG_HOME/language-review/config.toml` (или `~/.config/language-review/config.toml`) на Unix. Явно переданный `--config` объединяется поверх пользовательского файла; параметры CLI имеют наивысший приоритет. Значения `config show` сопровождаются источником. Поля, похожие на секреты, и параметры URL скрываются.

Пример TOML:

~~~toml
[scan]
respect_gitignore = true
max_file_size = 5000000
exclude_patterns = ["docs/generated/**"]

[doctor]
programs = ["vale"]
http_services = { ollama = "http://127.0.0.1:11434/api/tags" }

[profiles.rust]
priority = 20
project_markers = ["Cargo.toml"]
match_paths = ["crates/**"]
[profiles.rust.scan]
extensions = [".rs", ".md", ".txt"]
~~~

## Коды завершения

- `0` — команда выполнена; пропуски являются ожидаемым результатом инвентаризации.
- `2` — неверные аргументы, отсутствующий входной путь или ошибочная конфигурация.
- `3` — ошибка чтения/обхода, недоступный настроенный сервис или программа, неразрешимый указанный путь либо неполное/неподдерживаемое извлечение.
- `130` — операция прервана пользователем.

## Проверки разработки

Python-исходники, тесты и документация находятся в проекте. Временные окружения и результаты сборки не входят в исходники.

Порядок команд и критерии проверки собраны в [контексте проверок](.codex/context/verification.md). Скрипт `scripts/verify-package.ps1` собирает wheel и sdist, устанавливает их в отдельные временные среды и проверяет установленные entry point, `python -m language_review`, `scan` и `extract`.

## Границы поддержки

`scan` остаётся инвентаризацией и сохраняет свою схему. `extract` не вызывает языковые движки и не предлагает исправления. Tree-sitter грамматики поставляются как обычные runtime-зависимости и не скачиваются во время анализа. Markdown поддерживает заголовки, абзацы, списки, GFM-таблицы, подписи ссылок и inline-разметку; неоднозначное сопоставление с исходником помечается как частичный результат.
