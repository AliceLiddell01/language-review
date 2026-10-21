# Многоступенчатый анализ и Sign in with ChatGPT

Настройки языковой политики теперь задаются в обязательном корневом
`.language-review.yaml`. TOML-примеры ниже описывают прежнюю форму полей:
модели, режимы, бюджеты и термины перенесите в YAML-профиль; адреса сервисов
и отдельные согласия оставьте в пользовательском TOML. Точный контракт и
миграция — [проектная конфигурация](project-configuration.md).

Локальные LanguageTool/Vale/Gemma остаются умолчанием. Дополнительные проходы работают
в существующем `check`; исходники не исправляются. Качество на независимом реальном
русском gold пока **NOT_RUN**. Контрактные тесты и согласие моделей не доказывают его.

## Режимы

```powershell
language-review check .\repo --analysis-mode normal --view review --format json
language-review check .\repo --analysis-mode adaptive --strategy grammar --strategy punctuation
language-review check .\repo --analysis-mode deep --verify-hypotheses --view review --format json
```

`normal` делает один общий поиск. `adaptive` назначает дополнительные поиски по
наблюдаемым сигналам: low/medium confidence, rejected/незавершённый ответ, конкурирующие
варианты, конфликт с LT/Vale, риск правки, смыслочувствительная категория, disputed memory.
Любой сигнал назначает все выбранные стратегии этой единице. Это воспроизводимая
эвристика, не калиброванная вероятность. Причины и неназначенные единицы видны в отчёте.

`deep` назначает каждую стратегию **всем применимым LLM-единицам**, включая чистые.
Стратегии: `grammar`, `spelling`, `punctuation`, `technical` (стиль/термины), `meaning`
(двусмысленность/связность, категория style), `english`. Учитываются `kind`, `context`
и существующая применимость английской прозы. Явное сокращение списка уменьшает глубину.

`--verify-hypotheses` отдельно проверяет готовые гипотезы LLM и локальных движков:
цитату, категорию, контекст, варианты, отрицания, числа, идентификаторы и escape.
Цитаты локальных движков отображаются в LLM-виде по исходным диапазонам, поэтому
повторяющийся текст не делает найденную позицию неоднозначной. Непредставимая позиция
получает `not_applicable` с диагностикой и не переводит проверенную единицу в ошибку.
Решения `confirmed/rejected/uncertain` и свидетельства сохраняются в журнале. Они
не заменяют первичные evidence, не переписывают исправления и не являются человеческими
`accepted/rejected/disputed` или gold. Ошибка схемы/запроса и слишком большой контекст
делают верификацию неполной.

Повторы одной модели остаются источниками одного движка `llm`. Общая правка объединяется
с сохранением `rule_id` каждого прохода; competing/rejected не теряются. Проверки
локализации, масок, диапазонов и сохранения смысла действуют для всех ответов.

## Бюджеты и диагностика

```toml
[check.analysis]
mode = "adaptive"
strategies = ["grammar", "spelling", "punctuation", "technical", "meaning", "english"]
verify = true
max_passes = 8
max_requests = 100
max_text_chars = 200000
max_context_chars = 4000
timeout_seconds = 1800
concurrency = 1
retries = 0
```

В локальном `normal` неуказанные `max_requests`, `max_text_chars` и `timeout_seconds`
не задают общего лимита: сохраняется полнота прежнего однопроходного анализа. В `config show`
и `analysis_policy` отчёта отсутствие лимита представлено JSON `null`. В TOML для этого
нужно опустить ключ. Явно заданные бюджеты из пользовательского/явного конфига или CLI
действуют и в локальном `normal`, даже если равны приведённым выше значениям.
При выборе `adaptive`, `deep` или `chatgpt` неуказанные бюджеты получают приведённые
значения по умолчанию; смена режима/провайдера через CLI учитывается после загрузки конфигурации.

Действующие лимиты общие для первичного LLM-поиска, дополнительных поисков,
верификации и повторных попыток. `max_passes`
включает первичный поиск, каждую стратегию и ступень верификации; внутри неё возможны
несколько запросов на гипотезы. `max_requests` считает inference, не служебные OAuth/models HTTP.
`max_text_chars` считает сумму символов инструкций и JSON-входов попыток, без фиксированной
транспортной JSON-схемы; это не токены. `max_context_chars` ограничивает термины и
сериализованную память единицы во всех режимах. Термины сверх лимита блокируют её; такая
единица остаётся непроверенной и не считается чистой. Исключённые записи памяти считаются
отдельно. Этот же предел действует при верификации гипотез LanguageTool/Vale: чрезмерные
термины не отправляются модели, верификация остаётся неполной с `analysis_context_budget`,
а исходные свидетельства сохраняются. `llm.batch_chars/batch_items` ограничивают текст пакета;
длинный текст разбивается по предложениям с точными смещениями. Неразбиваемое предложение
остаётся непроверенным. Верификатор не обрезает большой контекст молча.

`llm.timeout_seconds` ограничивает запрос, `analysis.timeout_seconds` — общий бюджет
отправки; сериализация авторизации имеет отдельные ограниченные ожидания. `concurrency`
допускает 1–16 запросов поиска с обработкой в исходном порядке. Верификация последовательна.
При остановке уже начатые запросы завершаются в своём timeout. `retries` допускает 0–3
повтора только временных сетевых/5xx/rate-limit ошибок, с backoff. Usage limit, отказ
прав, неподдерживаемый параметр и неверный JSON не повторяются.

Первичная причина общей остановки сохраняется в `analysis.stop_reason` и журнале проходов;
оставшиеся единицы получают тот же код `analysis_*_budget`, а не `engine_aborted`.
Ограничение бюджета не считается сбоем транспорта. Три последовательных реальных сбоя
по-прежнему прекращают отправку с `engine_aborted`. Некорректный или чрезмерно вложенный
JSON верификатора даёт `verification_invalid_response` и `uncertain`, сохраняя кандидатов.

Планируйте `max_requests` по числу поисковых пакетов **и всех гипотез**, а не только
по числу проходов или итоговых замечаний. Один диагноз из трёх проходов сохраняет три
первичных свидетельства и требует трёх запросов верификации. Для наблюдаемого набора
минимум — `B + H`, где `B` — число поисковых запросов, `H` — число проверяемых гипотез.
Резерв на максимально разрешённые повторы — `(B + H) * (retries + 1)`; он не отменяет
лимиты времени/текста. До поисков число гипотез неизвестно; эта формула не гарантирует
полноту произвольного корпуса. Незавершённый запуск остаётся partial/unavailable.

В live-примере с `spelling` и `grammar` получились три поисковых запроса и шесть
гипотез: требуется девять запросов, хотя итоговых замечаний только два. Лимит восемь
не позволяет закончить этот набор даже без сетевых ошибок. Повтор с `max_requests=18`
и `retries=1` завершился девятью запросами без повторов, при прежнем
`llm.timeout_seconds=180`. Результат и границы диагностики —
[в матрице проверки](multistage-validation.md#завершение-большого-deep).

CLI переопределяет `--max-requests`, `--max-text-chars`, `--max-passes`. В
`engines[].details.analysis` находятся журнал проходов, routing, `units_complete/remaining`,
`chars_complete/remaining`. `request_log` показывает попытки, задержку и токены без
содержимого запросов. `transport_completed` подтверждает только транспорт; анализ
проверяется отдельно. Незавершённая обязательная ступень не даёт `clean`.

## OAuth в Windows

В рабочей копии установите необязательный crypto/JWT extra:

```powershell
uv sync --locked --extra chatgpt
uv run --locked language-review chatgpt login
uv run --locked language-review chatgpt status
uv run --locked language-review chatgpt models
```

`login` открывает системный браузер **Continue with ChatGPT**, слушая только
`127.0.0.1` на свободном порту, путь `/auth/callback`. Пользователь самостоятельно
авторизуется и разрешает использование плана. Ctrl+C, отказ и timeout сохраняют прежний
аккаунт. Используются dynamic client registration, постоянный host UUID, одноразовые
state/nonce/PKCE S256, проверка JWKS-подписи ID token, issuer/audience/expiry/nonce и subject.

Регистрации разделены по issued client ID и verified subject, даже при одинаковом
email. `status` показывает метаданные и разрешение плана. Вход без direct scope сохраняется,
но inference запрещена. Для выбора/повторного входа используйте метку из своего `status`:

```powershell
language-review chatgpt select account-0123456789ab
language-review chatgpt refresh
language-review chatgpt logout
language-review chatgpt login --account account-0123456789ab
language-review chatgpt login --account account-0123456789ab --consent
```

Повторный вход сохраняет client ID и host ID. `--consent` явно повторяет запрос
разрешения плана. Refresh сериализуется между процессами, новые токены/scopes/expiry
записываются атомарно. Терминальная ошибка refresh очищает токены, сохраняя регистрацию.
Logout пытается отозвать renewable session, затем очищает локальные токены; отсутствие
подтверждения отзыва сообщается. Доступ можно отключить в [ChatGPT Settings → Usage](https://chatgpt.com/settings/usage).

`%LOCALAPPDATA%\LanguageReview\chatgpt\accounts.dpapi`, включая временную запись,
защищён Windows DPAPI CurrentUser. Токены не попадают в конфиг, stdout, отчёты, PostgreSQL
или логи. Credentials Codex не читаются. Хранилище привязано к пользователю/машине;
на других ОС этот OAuth storage явно недоступен. Локальные движки не требуют crypto extra.

## Включение облака и модели

Наличие OAuth-сессии не разрешает отправку исходников:

```powershell
language-review check .\repo --engine llm --llm-provider chatgpt --allow-cloud `
  --model gpt-6-luna --reasoning-effort medium --analysis-mode adaptive `
  --view review --format json --output .\review.json
```

Либо задайте пользовательский конфиг:

```toml
[check.llm]
provider = "chatgpt"
timeout_seconds = 180
[check.llm.chatgpt]
allow_cloud = true
allow_memory = false
model = "gpt-6.1-sol"
effort = "high"
```

Перед проверкой загружается `/v1/models` выбранного аккаунта. `models` показывает
серверные display name/slug и `allowed`; выбор ограничен реально присутствующими
`gpt-6-luna` и `gpt-6.1-sol`. Отсутствующая модель даёт отказ. Модель, аккаунт,
провайдер и способ оплаты не переключаются автоматически.

| Экспериментальный профиль | Настройка |
| --- | --- |
| Массовый поиск | Luna low / medium |
| Точный поиск | Sol high; для A/B также medium |
| Сложная перепроверка | Sol high / явно выбранный xhigh |
| Локальный baseline | прежняя Gemma 4 E4B |

Effort Luna: `none/low/medium/high/xhigh/max`; Sol: `low/medium/high/xhigh/max`.
Документированная совместимость не гарантирует доступность OAuth/account. Каталог
не объявляет effort: metadata показывает `catalog_does_not_declare_efforts`, затем
результат реальной попытки. `effective_reasoning_effort` — null, если сервер его не
вернул. `max` включается явно и может расходовать больше квоты. Превосходство профилей
не доказано. Профиль применяется ко всем выбранным ступеням данного запуска.

Облачный транспорт использует системные настройки HTTPS proxy (включая Windows proxy),
сохраняя проверку TLS и allowlist адресов OpenAI; перенаправления запрещены.
Локальные движки по-прежнему обходят proxy. Если вход в браузере проходит, а приложение
получает `connection_failed`, проверьте доступность системного proxy для Python.

Транспорт использует только публичный `POST https://api.openai.com/v1/responses`,
`store=false`, `stream=true`, массив input, instructions и reasoning.effort. Поля
temperature/max_output_tokens/previous_response_id и инструменты не отправляются.
Схема передаётся инструкцией, без предположения о structured output через OAuth;
фактический JSON строго проверяется продуктом. Если заголовок Content-Type отсутствует,
принимается только корректный SSE-поток
с `response.completed`; явно неверный Content-Type отклоняется. Failed/incomplete,
обрыв SSE, timeout и неверный JSON не считаются чистым анализом.

При usage limit отправка прекращается. Доступные токены и latency отражены в отчёте;
процент квоты и стоимость остаются null. Тариф API не пересчитывается в подписочный
расход. Политики плана/кредитов принадлежат ChatGPT; резервного API key маршрута нет.

Recall использует прежние границы project/dataset и остаётся недоверенным контекстом.
Для передачи отобранной памяти в облако **дополнительно** нужны `--allow-cloud-memory`
или `allow_memory=true`. Snapshot фиксируется хешем; база и gold целиком не отправляются.
Автоматическая запись событий check сохранена. Фоновая консолидация через ChatGPT
в этом этапе запрещена даже при `allow_cloud=true`; worker использует локальный конфиг.

## Совместимость и измерения

Расширенные режимы/ChatGPT выдают check/review **1.1**; обычный локальный запуск сохраняет
1.0. Обе версии читают пакетные схемы, `show-review`, `verify-review` и существующий
evaluator. Старые примеры 1.0 остаются валидными. Старый reader может отвергнуть 1.1 —
обновите его. Журнал проходов в details — недоверенная диагностическая аннотация;
`verify-review` сверяет первичные локализованные evidence, а не диагноз верификатора.

Контракт промпта — `ru-technical-review/4`. Фиксируются fingerprints базового промпта,
стратегий/верификатора, политики документов, snapshot памяти, model/effort/capability,
requests/retries/latency/tokens и покрытие ступеней. Пример действительного CLI-отчёта
на синтетическом протокольном сервере — [multistage-review.example.json](multistage-review.example.json).

Используйте [существующий evaluator](evaluation.md): отдельные `run` с одним corpus/hash/split,
`--config` локального baseline и каждого Luna low/medium, Sol medium/high, `--repeat`,
`--product-commit`, новыми внешними каталогами `--output`; затем `compare` результатов.
Меняйте один фактор за раз: модель, effort, prompt, режим или память. Capture сохраняет
config hash/commit/corpus/split, scoring — telemetry и покрытие. Метки не передаются check.
Не настраивайте стратегии на frozen test. Human gold создаётся имеющимся annotation workflow.

Detection precision/recall/F0.5, pipeline recall, correction, FP, overediting и разрезы
форматов/категорий принадлежат evaluator. Непокрытые случаи не TN. Синтетика и внешние
exact-match corrections не заменяют независимую разметку. Реальные OAuth/catalog/inference
и сценарии CLI проверены на синтетических входах —
результаты в [матрице проверки](multistage-validation.md). Реальный human baseline
остаётся **NOT_RUN**; прирост качества не заявляется.

Внешний контракт сверяется с официальной документацией OpenAI:
[регистрация](https://developers.openai.com/siwc/token-sharing-open-source/sign-in),
[сессии](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions),
[inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference),
[ограничения](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations),
[Luna](https://developers.openai.com/api/docs/models/gpt-6-luna),
[Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol). Доступность аккаунта
определяет живой сервер.
