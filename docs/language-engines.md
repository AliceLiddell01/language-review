# Языковые движки для `check`

`language-review check` передаёт фрагменты, найденные `extract`, трём независимым движкам:

| Движок | Что проверяет | Как подключается |
| --- | --- | --- |
| LanguageTool | Орфография, грамматика, пунктуация, типографика по правилам и словарю | Локальный HTTP-сервер, `POST /v2/check` |
| Vale | Стиль и терминология по правилам из файлов YAML | Внешняя программа, машинный вывод `--output=JSON` |
| LLM (Gemma 4 E4B) | Грамматика, пунктуация, стиль, канцеляризмы, терминология, остаточный английский | `llama-server` из llama.cpp (по умолчанию) или Ollama |

Все адреса по умолчанию локальные. Нелокальный адрес отклоняется конфигурацией, пока явно не задано `check.allow_remote_endpoints = true`; в отчёте у каждого HTTP-движка есть `endpoint_origin` (`loopback` или `remote`). Публичный бесплатный API LanguageTool запрещает автоматическую массовую проверку — используй собственный сервер.

Движки выбираются явно: `check.engines` в конфиге или `--engine` в командной строке. Если выбранный движок недоступен, `check` не заменяет его другим и возвращает код `5`; LLM-провайдер (`llama_cpp` или `ollama`) тоже выбирается явно и не переключается при ошибке.

## Минимальный конфиг

~~~toml
[check]
engines = ["languagetool", "vale", "llm"]

[check.languagetool]
endpoint = "http://127.0.0.1:8081"

[check.llm]
provider = "llama_cpp"

[check.llm.llama_cpp]
endpoint = "http://127.0.0.1:8080"
# model = "gemma-4-e4b"  # нужен, если сервер предоставляет несколько моделей
~~~

`language-review doctor` проверяет каждый выбранный движок (готовность, модель, версию) и показывает применяемые настройки без секретов. `language-review config show` выводит все параметры `check` и их источники.

## Windows: llama.cpp `llama-server`

1. Установи llama.cpp: `winget install ggml.llamacpp` или архив релиза с <https://github.com/ggml-org/llama.cpp/releases>. Проверь: `llama-server --version`.
2. Возьми GGUF Gemma 4 E4B из <https://huggingface.co/ggml-org/gemma-4-E4B-GGUF> (например, вариант Q4_K_M). Можно указать уже скачанный файл.
3. Запусти сервер на loopback-адресе:

~~~powershell
llama-server -m D:\models\gemma-4-E4B-it-Q4_K_M.gguf --alias gemma-4-e4b `
  --host 127.0.0.1 --port 8080 -c 8192 -ngl 99 -np 1
~~~

`--alias` задаёт имя модели в `/v1/models`; если сервер предоставляет одну модель, `check.llm.llama_cpp.model` можно не задавать. `check` запрашивает `/health`, `/v1/models`, `/props` (версия сборки и квантование без пути к файлу) и `/v1/chat/completions` с `response_format` типа `json_schema`. Рассуждения модели по умолчанию выключены (`chat_template_kwargs.enable_thinking = false`): с ними Gemma 4 тратит сотни токенов до ответа. Включение — `check.llm.thinking = true`.

Порт 8080 часто занят другими программами. В этом случае `doctor` показывает `HTTP 404` или `model_not_found`: запусти сервер на другом порту и укажи его в `check.llm.llama_cpp.endpoint`.

### Фактические параметры на RTX 3060 Ti 8 ГБ

Измерено 2026-10-18 на Windows 11, llama.cpp b10299 (CUDA), Gemma 4 E4B Q4_K_M (7,5 млрд параметров, файл 5,5 ГБ), `-c 8192 -ngl 99 -np 1`:

- модель загружается за ~15 с; занятая видеопамять — 4,3 ГБ из 8 ГБ (~2,5 ГБ было занято до запуска);
- без рассуждений обработка запроса — ~850 токенов/с, генерация — ~60 токенов/с;
- пакет из 4–8 коротких фрагментов обрабатывается за 1,5–6 с; 16 единиц семи файлов разных форматов — за ~19 с.

Это результаты одной машины, а не гарантии: параметры `-c`, `-ngl`, `check.llm.batch_chars`, `check.llm.batch_items` и `check.llm.max_output_tokens` подбирай по своей видеокарте. Если ответ обрезается (`llm_truncated_response`), увеличь `max_output_tokens` или уменьши `batch_chars`; если не хватает контекста (`http_400`), уменьши `batch_chars` или увеличь `-c`.

## Windows: LanguageTool

Нужна Java 17 или новее.

1. Скачай `LanguageTool-*.zip` с <https://languagetool.org/download/> и распакуй.
2. Запусти локальный сервер (<https://dev.languagetool.org/http-server>):

~~~powershell
java -cp languagetool-server.jar org.languagetool.server.HTTPServer --port 8081
~~~

Сервер по умолчанию слушает только локальный интерфейс. `check` отправляет `language=ru-RU` (параметр `check.languagetool.language`), читает правила, категории, варианты замены и позиции. Позиции LanguageTool считаются в UTF-16 code units — `check` переводит их в символы и затем в координаты файла. Варианты словаря (`origin = "dictionary"`) перечисляются без выбора «правильного»: первый вариант не обязательно верный.

Отключить правило можно через `check.languagetool.disabled_rules = ["RULE_ID"]`; режим `check.languagetool.level = "picky"` включает дополнительные правила.

## Windows: Vale

Установи Vale: `winget install errata-ai.Vale` или архив релиза с <https://github.com/vale-cli/vale/releases>. Проверь: `vale --version`.

Без дополнительной настройки `check` использует встроенный стиль `LanguageReview` из пакета: временная конфигурация создаётся при каждом запуске, глобальная конфигурация Vale и переменные `VALE_CONFIG_PATH`/`VALE_STYLES_PATH` игнорируются. Правила стиля:

| Правило | Что находит | Пример |
| --- | --- | --- |
| `Terminology` | Жаргонные заимствования с заменой в той же грамматической форме | «юзеров» → «пользователей», «кликните» → «нажмите» |
| `Jargon` | Жаргонизмы без однозначной замены | «дефолтный», «конфиг» |
| `Bureaucratese` | Канцеляризмы | «осуществляется», «посредством» |
| `Repetition` | Повтор русского слова подряд | «в в», «не не» |
| `Dash` | Дефис с пробелами вместо тире | «файл - пример» |

Vale — инструмент правил стиля, а не грамматический корректор русского языка. Терминологию проекта задают данными:

~~~toml
[check]
terms = { "фреймворк" = "каркас" }

[profiles.docs.check]
terms = { "апдейт" = "обновление" }
~~~

Термины сравниваются как целые слова без учёта регистра и передаются также в промпт LLM. Собственную конфигурацию Vale со своими стилями задаёт `check.vale.config = "путь/к/.vale.ini"`; в ней должна быть секция `[*.txt]` или `[*]`, потому что фрагменты передаются как обычный текст. Позиции Vale (`Line`, `Span`) — строки от 1 и колонки в code points от 1 с включительным концом; `check` сверяет `Match` с текстом и отбраковывает несовпадение.

## Альтернатива: Ollama

~~~powershell
ollama pull gemma4:e4b
~~~

~~~toml
[check.llm]
provider = "ollama"

[check.llm.ollama]
endpoint = "http://127.0.0.1:11434"
model = "gemma4:e4b"
~~~

Или разово: `language-review check .\repo --llm-provider ollama`. Ollama получает тот же системный промпт и ту же JSON Schema в поле `format` запроса `/api/chat` (<https://docs.ollama.com/capabilities/structured-outputs>), `think = false`, `temperature = 0`. Ollama применяет собственный шаблон чата и параметры среды выполнения, поэтому ответы на одних и тех же весах могут отличаться: в нашей проверке Ollama 0.40.6 нашёл те же 4 ошибки на положительных примерах, что и llama-server, но дал 3 ложных стилистических замечания на чистом тексте, где llama-server не дал ни одного.

## WSL2 и Linux

Продуктовый код не зависит от ОС: в WSL2 или Linux устанавливается тот же пакет, а движки задаются адресами. Если WSL2 настроен с `networkingMode=mirrored`, сервисы Windows доступны по `127.0.0.1` и конфиг не меняется:

~~~bash
uv sync --locked
uv run language-review --config ./language-review.toml check ./repo
~~~

В режиме NAT адрес Windows-хоста не loopback: укажи его явно и включи `check.allow_remote_endpoints = true`, понимая, что тексты уходят по сети. Vale для Linux устанавливается отдельно (пакет дистрибутива или релиз Vale).

## Типовые ошибки

| Диагностика | Причина | Что сделать |
| --- | --- | --- |
| `connection_failed` | Сервис не запущен или неверный порт | Запусти сервер, проверь `endpoint` |
| `http_404` | По адресу отвечает другая программа | Смени порт сервера и `endpoint` |
| `model_loading` | `llama-server` ещё загружает модель (`/health` = 503) | Подожди и повтори |
| `model_not_found`, `model_not_selected` | Модель не загружена или не выбрана | Проверь `--alias`/`ollama list`, задай `model` |
| `language_not_supported` | Сервер LanguageTool без русского языка | Используй полный дистрибутив LanguageTool |
| `program_not_found` | Vale не найден в `PATH` | Установи Vale или задай `check.vale.program` |
| `vale_E100` и другие `vale_*` | Ошибка конфигурации Vale | Проверь `check.vale.config` и `StylesPath` |
| `llm_invalid_json`, `llm_schema_violation` | Ответ модели не соответствует схеме | Проверь модель и версию сервера; результат не считается чистым |
| `llm_truncated_response` | Ответ обрезан лимитом токенов | Увеличь `max_output_tokens` или уменьши пакет |
| `engine_aborted` | Три подряд сбоя транспорта | Проверь сервис; оставшиеся фрагменты помечены как непроверенные |
| `unit_too_large` | Единица проверки длиннее 20 000 символов | Разбей текст; единица не проверена |

## Приёмочные проверки на реальных движках

Тесты `tests/test_check_live.py` запускаются только при заданных адресах и пропускаются без них:

~~~powershell
$env:LANGUAGE_REVIEW_LIVE_LANGUAGETOOL = 'http://127.0.0.1:8081'
$env:LANGUAGE_REVIEW_LIVE_LLAMA_CPP = 'http://127.0.0.1:8080'
$env:LANGUAGE_REVIEW_LIVE_OLLAMA = 'http://127.0.0.1:11434'
$env:LANGUAGE_REVIEW_LIVE_OLLAMA_MODEL = 'gemma4:e4b'
uv run --locked pytest tests/test_check_live.py -ra
~~~

Vale используется, если программа найдена в `PATH`. Пропущенный тест означает `NOT_RUN`, а не успешную проверку.
