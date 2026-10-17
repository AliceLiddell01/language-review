---
name: language-review-git-workflow
description: >-
  Единый workflow Git/GitHub для Language Review: ветки, staging, commit, push,
  точная проверка remote SHA, PR body, Draft/Ready, конфликты и отдельно
  разрешённый merge. Используй при любой задаче, дошедшей до публикации.
---

# Git и PR — владелец жизненного цикла

## Scope и маршрутизация

Этот skill владеет branch/index/commit/push/PR/remote postconditions, а не содержанием кода, CI или CodeRabbit CLI. При начале Git/PR этапа прочитай [pr-lifecycle.md](references/pr-lifecycle.md); перед созданием/редактированием PR — [pr-body.md](references/pr-body.md). CodeRabbit review обрабатывает отдельный skill. Не включай в промпты искусственный запрет push.

## Предусловия

1. Установи фактический checkout, `git status --porcelain=v1`, текущий branch, remotes, base ref и точный HEAD. Не угадывай `origin`, имена default branch, user identity, права или то, что локальные изменения принадлежат текущей задаче.
2. Не уноси посторонние uncommitted файлы в PR. Сверяй task scope, `git diff --check` и релевантную verification из `.codex/context/verification.md`.
3. Одна самостоятельная публикуемая задача — одна ветка + один PR. Если branch существует, работай с ним; при новой задаче создавай ветку от актуальной default branch. Не перезаписывай чужую branch или историю.
4. Продолжай штатный workflow после commit: push, exact remote SHA, создать/обновить PR, перечитать опубликованный PR и подтвердить base/head. Локальный commit не означает опубликованный результат.

## Безопасная публикация

- Staging только по принадлежащим задаче путям (не `git add -A` вслепую); сравни staged diff. `uv.lock` обновляй штатным `uv lock`, а не руками.
- Не используй force-push, `--force-with-lease`, reset либо merge/rebase опубликованной ветки автоматически для обхода проблем. При конфликтах выяви изменения обеих сторон, разреши без потерь и снова проверь.
- Не создавай дублирующий PR. Тело содержит проверяемые разделы, реальные команды, PASS/FAIL/PARTIAL/NOT_RUN и пределы доказательств. Отмечай изменения продукта отдельно от инфраструктуры.
- Merge, удаление ветки и очистка после merge — только по отдельному актуальному разрешению пользователя.

## Завершение

Сообщи branch, commit SHA, remote SHA, URL/номер PR, CI evidence или NOT_RUN, изменённые файлы и blocker. Для remote-only GitHub connector проверяй создаваемый tree/commit/ref через API и перечитывай diff/PR.
