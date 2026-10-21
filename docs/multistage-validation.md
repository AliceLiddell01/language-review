# Проверка многоступенчатого анализа

Среда: Windows, PowerShell, Python 3.15.0, uv; тесты выполняются с восемью workers.
Протокольные HTTP/SSE-ответы, JWT-ключи, корпуса и credentials в тестах синтетические.
DPAPI, loopback listener, межпроцессная блокировка и установленный CLI — настоящие.
Самопроверка не является независимым ревью или человеческой разметкой.

## Матрица требований

| Требование | Наблюдаемое подтверждение |
| --- | --- |
| PKCE/state/nonce/JWKS, dynamic/returning client | `test_dynamic_registration_uses_issued_client_pkce_and_resource`, `test_callback_errors_never_exchange_tokens`, `test_oidc_rejects_wrong_claims`, `test_oidc_rejects_invalid_signature_and_unknown_key`, `test_returning_registration_reuses_client_and_rejects_client_switch` |
| Loopback, отмена, новый code после invalid_grant | `test_loopback_login_and_new_code_recovery`, `test_cancelled_and_timed_out_login_preserve_active_account` |
| Ротация, отказ, независимые аккаунты, logout | `test_token_scopes_identity_and_rotation`, `test_refresh_terminal_error_clears_only_tokens_and_retains_registration`, `test_refresh_network_failure_preserves_session`, `test_logout_clears_tokens_and_reports_remote_revocation`, `test_model_catalog_and_requests_pin_selected_account` |
| DPAPI и refresh между процессами | `test_windows_dpapi_storage_and_concurrent_refresh`, `test_windows_refresh_is_serialized_across_processes`, `test_corrupt_protected_storage_is_diagnostic`, `test_credentials_cannot_be_saved_in_a_repository` |
| Публичный OAuth Responses, каталог, параметры, JSON | `test_responses_public_route_allowed_payload_and_completed_json`, `test_cloud_consent_and_model_catalog_no_fallback`, `test_cloud_product_check_validates_stream_and_same_review_contract`, `test_model_effort_validation` |
| Обрыв/failed/incomplete, неверный SSE, ошибки без утечек | `test_sse_failures_never_return_partial_text_as_success`, `test_invalid_sse`, `test_http_errors_redact_detail_and_preserve_code_shape`, `test_malformed_sse_discriminator_is_diagnostic`, `test_nonstring_error_parameter_does_not_escape_diagnostics` |
| Deep проверяет clean, adaptive объясняет routing | `test_deep_search_includes_clean_units_and_preserves_pass_sources`, `test_adaptive_routing_uses_observed_uncertainty_and_explains_omissions`, `test_adaptive_conflict_with_local_engine_is_routed` |
| Бюджеты, concurrency, retry/quota не дают clean | `test_deep_budgets_never_claim_clean`, `test_parallel_requests_respect_concurrency_and_global_budget`, `test_transient_retry_counts_actual_calls_once_and_quota_stops_remaining` |
| Поиск отдельно от верификации; evidence сохранены | `test_verifier_keeps_original_hypothesis_and_does_not_rewrite`, `test_verifier_invalid_quote_and_timeout_are_incomplete`, `test_wrong_quote_rejected_in_each_pass`, `test_extra_search_keeps_meaning_and_technical_rejections`, `test_concurrent_variants_and_same_error_across_documents_are_preserved` |
| Память не получает нового source of truth и облачного worker | `test_existing_memory_recall_obeys_separate_cloud_consent`, `test_background_memory_cannot_inherit_cloud_consent`; прежние тесты project/dataset isolation сохранены |
| Evaluator читает 1.1, metadata/coverage и A/B | `test_evaluator_compares_new_reports_and_preserves_pass_telemetry`; сравниваются разные реальные выдачи продукта на synthetic contract corpus, без заявления о языковой точности |
| CLI/doctor/config и автономный локальный режим | `test_cli_cloud_consent_config_and_doctor_are_observable`, `test_default_local_uses_single_search_and_no_oauth`; существующие `test_config.py`, `test_doctor.py`, `test_review.py` |
| Установленные wheel/sdist и optional crypto | `scripts/verify-package.ps1`: CLI/module, scan/extract/check/review/eval, отказ облаку без согласия до установки extra, установка `[chatgpt]`, DPAPI round-trip, пустое изолированное хранилище, сохранность источника |

## Границы результатов

- **PASS, контракт:** поисковые режимы, бюджеты, защищённые диапазоны, синтетические
  OAuth/Responses и совместимость старых отчётов. Пример
  [multistage-review.example.json](multistage-review.example.json) получен фактическим CLI:
  первый поиск чистый, второй находит гипотезу; `verify-review` подтвердил исходные байты.
- **PASS, Windows:** настоящий DPAPI и сериализация refresh двух процессов; crypto/JWT
  extra устанавливается из wheel и sdist, базовая установка обходится без него.
- **PASS, live OAuth:** пользователь самостоятельно вошёл через продукт; реальные
  `/v1/models`, refresh и Luna/Sol inference подтверждены. Сценарии и ограничения
  приведены ниже. Credentials Codex и API keys не используются.
- **NOT_RUN, human baseline:** независимый реальный русский gold не предоставлен.
  Detection precision/recall/F0.5, correction, FP/overediting и прирост от режимов/памяти
  не измерены. Протокольные сценарии не являются заменой этих метрик.
- **NOT_RUN, live LT/LLM/PostgreSQL:** отсутствуют параметры изолированной БД и live
  тестов движков. Read-only probes дали LT `connection_failed`, настроенной llama.cpp
  `http_404`, Ollama `model_not_found` для Gemma 4 E4B. Рабочая доступность Vale проверяется
  имеющимся пакетным smoke. Сервисы/модели не устанавливались и не подменялись.

Канонические команды и статусы — [verification](../.codex/context/verification.md).
Пользовательский запуск и ограничения — [руководство](multistage-analysis.md).
Фактические результаты полного прогона и CI привязываются к опубликованному HEAD в PR.

## Реальные проверки ChatGPT

Дата: 2026-10-20. Windows / PowerShell / Python 3.15.0. Пользователь лично выполнил
Sign in with ChatGPT; затем запускался настоящий CLI на внешних синтетических входах.
Исходники сторонних проектов и предметная память не отправлялись; `--no-memory`.
OAuth-сессия остаётся подключённой для последующих пользовательских проверок.

| Сценарий | Результат |
| --- | --- |
| Browser OAuth, DPAPI session, `/v1/models`, явный refresh | PASS; каталог содержит обе разрешённые модели |
| Luna medium, normal, Markdown | PASS; 1 запрос, 4/4 единицы, 2 замечания, 1300 input / 113 output tokens |
| Sol high, normal, Markdown/Rust/C#/C++ | PASS; 4 запроса, 7/7 единиц, 5 замечаний, 4864 input / 287 output tokens |
| Luna low, adaptive, Rust | PASS; 1 запрос, 1/1 единица, 1 замечание; дополнительные стратегии имеют `adaptive_no_signal` |
| Sol high, deep, spelling + verification, Rust | PASS; 4 запроса, 1/1 единица, две гипотезы проходов confirmed, 3232 input / 230 output tokens |
| Sol high, deep, grammar + spelling + verification, Markdown | PARTIAL; 8 запросов, один timeout 180 секунд и затем `analysis_request_budget`; 3/4 единицы полны, первичные evidence сохранены |
| OAuth есть, cloud consent выключен | PASS; `cloud_consent_required`, 0 запросов, результат unavailable |
| Luna low, deep, max_requests=1 | PASS для ограничения бюджета; ровно 1 запрос, сохранены 2 замечания, обязательный проход остановлен с `analysis_request_budget`, результат unavailable, не clean |

Обнаружены и исправлены две реальные несовместимости: облачный транспорт обходил
системный proxy Windows, а корректный SSE-поток без Content-Type отвергался до чтения.
Проверка TLS/allowlist и запрет redirects сохранены; локальные движки обходят proxy.
`show-review` прочитал все семь сохранённых отчётов, `verify-review` подтвердил
`current`; SHA-256 всех четырёх исходных файлов остались прежними.
Регрессии: `test_cloud_https_uses_system_proxy_without_exposing_bearer`,
`test_sse_without_content_type_still_requires_completed`,
`test_nonstream_json_is_never_a_completed_response`.

Эти результаты подтверждают работоспособность транспорта и продуктового конвейера,
но не precision/recall на независимом корпусе. Число замечаний не является метрикой
качества; межмодельное сравнение на этих разных входах не выполняется. Отзыв сессии,
смена аккаунта и принудительное исчерпание квоты в live не запускались.

## Завершение большого deep

Повторён тот же синтетический `sample.md` с тем же SHA-256, моделью `gpt-6.1-sol`,
effort `high`, режимом `deep`, стратегиями `spelling`, `grammar` и полной верификацией.
Вход, промпты, число проходов и проверяемые гипотезы не сокращались; память выключена.
Исходный PARTIAL выше остаётся фактом первой попытки.

Причины исходной неполноты:

- `max_requests=8` был ошибкой настройки live-сценария: три поисковых пакета дали
  шесть гипотез (по две в каждом проходе), требующих шести отдельных верификаций.
  Нужно минимум девять запросов. Даже без timeout бюджет остановил бы последнюю гипотезу.
- Восьмой inference-запрос закончился `timeout` через 181110,9 мс при лимите 180 секунд.
  Это таймаут отдельного запроса, не общий `analysis.timeout_seconds=1800`.
  `retries=0` не разрешал повтор. Предыдущий журнал не содержит данных о ходе SSE,
  достаточных для установления причины задержки на стороне сервера или сети/proxy;
  такие причины не считаются доказанными.

Изменены только настройки повторного запуска: `max_requests=18` (девять операций
с резервом на один повтор каждой), `retries=1`. Оба таймаута сохранены. Настройки
по умолчанию и runtime-код продукта не менялись.

| Проверка | Результат |
| --- | --- |
| Итоговый review / движок | PASS: `findings` / `ok`, код CLI 1 означает найденные замечания |
| Поиск | PASS: primary, spelling, grammar; каждый проверил все 4/4 единицы, включая clean |
| Верификация | PASS: все 6 гипотез `confirmed`, ни одна не пропущена; это решения LLM, не human gold |
| Покрытие | PASS: `units_complete=4`, `units_remaining=0`, `chars_remaining=0` |
| Запросы / повторы / сбои | PASS: 9 / 0 / 0; восьмой запрос завершился за 6310,1 мс |
| Время движка / токены | 48257,4 мс; 6498 input / 920 output tokens |
| Чтение / актуальность | PASS: `show-review` прочитал оба замечания, `verify-review` вернул `current`; SHA-256 исходника сохранён |

Успешный повтор подтверждает достаточность бюджета и полное завершение прежнего
сценария с тем же таймаутом. Задержка первого запуска не воспроизвелась; её конкретная
сетевая/серверная причина остаётся неизвестной. Разрешённый retry в этом live-прогоне
не понадобился; его контракт проверяется существующими тестами, а не этим успехом.

Для воспроизведения сохраните конфиг вне исследуемого каталога:

```toml
[check]
engines = ["llm"]
[check.analysis]
mode = "deep"
strategies = ["spelling", "grammar"]
verify = true
max_requests = 18
max_text_chars = 60000
max_passes = 8
timeout_seconds = 1800
concurrency = 1
retries = 1
[check.llm]
provider = "chatgpt"
timeout_seconds = 180
batch_chars = 1500
batch_items = 8
[check.llm.chatgpt]
model = "gpt-6.1-sol"
effort = "high"
allow_cloud = true
allow_memory = false
```

```powershell
language-review --config .\deep-live.toml check .\input\sample.md --no-memory `
  --view review --format json --output .\review.json
language-review show-review .\review.json --all
language-review verify-review .\review.json --root root-0001=.\input\sample.md --format json
```

Точные синтетические входы, конфиг и реальные отчёты сохраняются во внешнем локальном
архиве приёмки; OAuth-хранилище и пользовательские данные в него не входят.
