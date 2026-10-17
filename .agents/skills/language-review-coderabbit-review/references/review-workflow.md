# CodeRabbit — checklist исполнителя

- Preflight: текущая Windows PowerShell, `coderabbit --version` и `coderabbit --help`, `git status`, `git rev-parse HEAD`, реальный PR и remote SHA. В случае удалённого API без доступа к локальному CLI не заявляй, что прогон CodeRabbit выполнен.
- Определи сравниваемую базу из актуального PR, не из жестко зашитой ветки. Проверь полный scope diff, особое внимание `scanner.py`, `config.py`, `report.py`, `doctor.py`, `tests/`, `uv.lock` и agent infrastructure.
- Не передавай приватные содержимое пользовательских репозиториев, локальные результаты или токены публичным сервисам. Запрашиваемый CLI review касается committed исходников Language Review.
- Отдели provider evidence, triage и remediation. Обоснование false positive должно ссылаться на конкретный код и требования.
- При реальном CodeRabbit finding проверяй `language-review-code-review` критерии; при отсутствии проблемы не создавай косметические изменения.
- Новая итерация возможна только после завершения предыдущей (верификация и удалённая публикация).
- Итоговый PR record: `| Итерация / reviewed HEAD | Уровень | Путь | Влияние | Решение | Исправление |`; указывай подтверждённое влияние и судьбу каждого существенного замечания, не маскируй блокеры.
