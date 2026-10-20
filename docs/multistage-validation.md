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
- **NOT_RUN, live OAuth:** собственных OAuth-регистраций приложения на проверяемом
  хосте нет. Реальные `/v1/models`, Luna/Sol inference и совместимость effort конкретного
  плана не подтверждены. Нет доступа к credentials Codex или чужим аккаунтам.
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
