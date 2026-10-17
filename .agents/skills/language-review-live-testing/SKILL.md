---
name: language-review-live-testing
description: >-
  Фактические smoke, CLI и acceptance-прогоны Language Review на установленном
  Python-пакете, внешних тестовых входах и реальном Windows окружении.
  Используй для живой проверки поведения, packaging и отказов.
---

# Живое и приёмочное тестирование

## Главный инвариант

Объект тестирования — настоящий установленный `language-review`, его `scan`, `doctor`, `config show`, `python -m language_review`. Временный helper может запускать, наблюдать, сравнивать и собирать результаты, но не заменяет реальный scanner собственным Python-обходом, не подменяет возвращаемые JSON/exit codes и не ослабляет проверки ради PASS.

## Подготовка

- Установи проверяемый commit, Windows/PowerShell среду, Python/uv versions, требования и реальный expected state.
- Убедись, что fixtures синтетические и не содержат текстов из сторонних приватных репозиториев. Создай отдельный безопасный временный каталог вне checkout; не помещай служебные файлы в `src/`, `tests/` или `.codex/context/`.
- Зафиксируй before/after проверяемых входов и config; `scan` должен оставаться read-only, кроме явно запрошенного пути `--output`.
- Применяй [acceptance-procedure.md](references/acceptance-procedure.md), каноническую verification из `.codex/context/verification.md` и существующий `scripts/verify-package.ps1`.

## Доказательство

Проверяй код возврата, stdout/stderr, JSON структуру, реальные файловые эффекты и обработку failures; не считай успешную работу test-wrapper доказательством успеха приложения. Проверки на Rust/C#/C++ — синтетические `scan`-входы, а не компиляция стороннего проекта.

## Завершение

Отдельный PASS/FAIL/PARTIAL/NOT_RUN по каждому существенному сценарию, фактическое окружение, отрицательные кейсы, остаточные риски, очистка внешнего temp. Не публикуй пользовательские inputs/секреты в GitHub artifacts.
