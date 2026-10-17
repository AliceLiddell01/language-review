# Language Review

Language Review — локальная CLI-утилита для инвентаризации текстовых файлов перед будущим языковым ревью русскоязычного текста в исходниках и документации.

## Возможности этого этапа

Сейчас утилита перечисляет поддерживаемые файлы, определяет тип файла, кодировку с учётом BOM, переводы строк, размер и базовую читаемость. Она умеет показывать эффективную конфигурацию и проверять её вместе с доступностью явно настроенных внешних программ и локальных HTTP-сервисов.

Поиск языковых ошибок, разбор Markdown и AST, вызов LLM, LanguageTool или Vale, SARIF, анализ Git diff и применение исправлений пока не реализованы. Команды `check` нет; отчёт `scan` содержит только инвентаризацию.

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
~~~

Формат по умолчанию — человекочитаемый. JSON содержит `schema_version`, сведения о программе и корнях, массив `files`, итоговые счётчики и статус обхода. Записи файла имеют состояния `selected`, `skipped` или `error`; языковых находок в схеме нет. При `--format json` stdout содержит только JSON, диагностика выводится в stderr.

По умолчанию учитываются `.rs`, `.cs`, `.cpp`, `.cc`, `.cxx`, `.c`, `.h`, `.hpp`, `.hh`, `.hxx`, `.md`, `.markdown`, `.txt`, `.rst` и `.adoc`. Новые расширения можно добавить через `--extension .toml`; `--only-extension` задаёт список вместо настроенного. Файлы с неподдерживаемым расширением, бинарным содержимым, неподходящей кодировкой или превышением лимита попадут в список пропусков с причиной.

Каталоги зависимостей и сборки, в том числе `.git`, `.venv`, `node_modules`, `target`, `bin`, `obj`, `build` и `dist`, исключаются по умолчанию. Для пользовательских исключений используй `--exclude 'docs/generated/**'`. Также поддерживаются `.gitignore` и отрицательные правила Git; обход ссылок выключен по умолчанию. `--follow-symlinks` включает ссылки только в пределах явно указанного корня и защищает от циклов.

Для стандартной конфигурации выбирай проектный профиль по маркерам, заданным в пользовательском конфиге; `--profile NAME` выбирает профиль явно. Профили с `match_paths` могут задавать настройки для относительных путей внутри корня сканирования.

### Диагностика

~~~powershell
language-review doctor
language-review doctor .\src
language-review doctor --format json
~~~

`doctor` проверяет установленный пакет, версию Python, загруженную конфигурацию, заданные пути, программы из `doctor.programs` и loopback HTTP-адреса из `doctor.http_services`. Недоступность необязательной программы не мешает `scan`, но отражается в результате диагностики и коде завершения.

### Конфигурация

~~~powershell
language-review config show
language-review config show --format json
language-review --config .\language-review.toml scan .\repo
~~~

Пользовательский конфиг хранится в `%APPDATA%\language-review\config.toml` на Windows и в `$XDG_CONFIG_HOME/language-review/config.toml` (или `~/.config/language-review/config.toml`) на Unix. Явно переданный `--config` объединяется поверх пользовательского файла; параметры CLI имеют наивысший приоритет. Значения `config show` сопровождаются источником. Секретоподобные поля и query-параметры URL скрываются.

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
- `3` — ошибка чтения/обхода, недоступный настроенный сервис или программа либо неразрешимый указанный путь.
- `130` — операция прервана пользователем.

## Проверки разработки

Python-исходники, тесты и документация находятся в проекте. Временные окружения и результаты сборки не входят в исходники.

~~~powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -n 8
.\scripts\verify-package.ps1
~~~

Smoke-скрипт строит wheel и sdist, устанавливает их в отдельные временные окружения и запускает установленные команды и модуль.

## Следующий этап

Следующий этап получает стабильный контракт инвентаризации: относительный путь в пределах корня, тип файла, статус и причина пропуска, размер, кодировка, BOM, переводы строк и версия схемы JSON. Извлечение текстовых фрагментов и языковые находки должны добавляться отдельно, не меняя смысл этих полей.
