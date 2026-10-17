---
name: language-review-coderabbit-review
description: >-
  Только явно запрошенный цикл CodeRabbit CLI на опубликованном HEAD Language
  Review: завершённое review, независимая проверка каждого finding, фиксы,
  verification, commit/push и накопительный PR record. Не автозапускать.
---

# CodeRabbit review cycle

## Активация

Только явная просьба «запусти/продолжи CodeRabbit», число итераций, deep review либо focus. Само упоминание CodeRabbit, редактирование `.coderabbit.yaml` или обычный code review **не** являются разрешением запуска. По умолчанию три завершённые итерации, если пользователь не указал другое положительное число. Две волны по пять — отдельный пользовательский workflow, не автоматический background job.

## Контракт цикла

1. Прочитай `AGENTS.md`, применимый context, [review-workflow.md](references/review-workflow.md) и `language-review-git-workflow`. Проверь текущий checkout, published HEAD, PR и установленный CLI; обнаружь **реальные** флаги через `--help`, не выдумывай.
2. Работай с опубликованным committed HEAD; не проверяй скрытые рабочие изменения как опубликованный review. Не запускай несколько одновременных review и не сокращай scope молча.
3. Запускай requested normal/deep/focus режим (без явного запроса deep не включать). Проверяй завершённость и охват provider output; exit code 0 недостаточно для признания review завершённым.
4. Для каждого finding установи `confirmed`, `partially confirmed`, `stale/repeated` либо `false positive` по reviewed HEAD, calls/tests, контракту и реальному техническому эффекту. Не исполняй сгенерированный patch вслепую.
5. Исправляй лишь подтверждённые root causes без ослабления тестов; делай нужную Python/Windows verification. После этого — отдельный commit, push через Git workflow, remote SHA postcondition, накопительное обновление PR body, повторное чтение PR и clean worktree.
6. Следующую итерацию начинай только после успешного завершения предыдущей. Исправленный commit **не** является reviewed SHA предыдущей итерации.

## Clean, errors, credits

Полностью завершённый review без подтверждённых замечаний может получить marker `git commit --allow-empty` только при доказанном clean result и в рамках согласованной серии итераций. Rate limit, incomplete provider output, authentication failure, paid confirmation, failed verification или push — это NOT_COMPLETED, не clean. Используй провайдерский retry интервал, без частого polling. Никогда не включай billed/credits режим самостоятельно.

Запрещены: автоматическое изменение `.coderabbit.yaml` по замечанию review, force-push, merge без разрешения, подмена CLI API, выдуманные результаты. Отчёт: запрошено/завершено итераций, reviewed SHA, число findings по disposition, публикация и оставшиеся blockers.
