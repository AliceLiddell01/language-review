# Контракт дистрибуции

- Обычный CI/main не выпускает продукт. Подготовка кандидата и публикация — разные
  ручные действия; version/channel/target SHA/previous stable задаёт инициатор.
  Реальный тег/Release требует отдельной актуальной команды пользователя.
- `scripts/release.py` извлекает точный commit во временное дерево; меняет только
  release-specific package metadata/identity/skill. `uv.lock` исходного SHA владеет
  runtime dependencies. Manifest связывает SemVer, PEP 440, tag, SHA и assets hashes.
- `release_transport.py` доверяет официальному GitHub API/TLS и проверяет integrity;
  это не независимая подпись. Не ослаблять HTTPS/redirect/asset boundaries ради тестов.
- `distribution.py` владеет отдельными immutable runtime, active pointer, journal,
  PATH и hashes глобальных skills. Чужие/изменённые файлы не перезаписываются.
  `setup_cli.py` меняет только среду пользователя, не создаёт проектный YAML,
  не выдаёт cloud/cloud-memory consent и не хранит глобальные модели/prompts.
- Worker устанавливается через Python API и получает релизный wheel из проверенного
  release directory; обновление CLI не обновляет БД/worker.
  Миграции БД не откатываются вместе с CLI.
- Пользовательский skill — ресурс `distribution_assets/SKILL.md`, а не development
  skills `.agents/skills/language-review-*`. Схемы YAML/review не зависят от SemVer.

Процедура и доверие: [выпуск](../../docs/releases.md),
[установка](../../docs/installation.md). Windows acceptance:
`scripts/verify-distribution.py`; обычная пакетная проверка остаётся отдельной.

Активная установка с legacy launcher отклоняется до изменений. Порядок перехода описан
в [руководстве по установке](../../docs/installation.md).
