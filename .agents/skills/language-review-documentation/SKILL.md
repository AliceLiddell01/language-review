---
name: language-review-documentation
description: >-
  Размещает новое долговременное знание Language Review между machine-readable
  source, кратким AGENTS.md, agent-context, docs и skills; устраняет дубли,
  устаревшие утверждения и правила о несуществующем функционале.
---

# Размещение и сопровождение знаний

При изменении документации или постоянных инженерных правил сначала классифицируй факт, затем выбирай владельца:

- Source/manifests/tests — точные значения и машинные контракты.
- `AGENTS.md` — только общие invariants и routing, полезные почти всегда.
- `.codex/context/` — короткий capability-specific критический для агента контракт.
- `docs/` или `README.md` — rationale, usage, onboarding, troubleshooting, примеры, acceptance evidence.
- `.agents/skills/` — повторяемые процедуры, а не постоянные архитектурные числа.

У одного факта один владелец; другие места ссылаются на него. В agent-context не фиксируй PR/commit, временную задачу, запуск CodeRabbit, конкретный ПК, log или план этапа. Не объявляй реализованной способность, которой ещё нет; `README.md` отражает состояние `scan`, `doctor`, `config show`.

Перед завершением проверь маршруты из `AGENTS.md` и `.codex/context/INDEX.md`, frontmatter/links skills, отсутствие битых относительных ссылок и дублирующих команд, соответствие examples фактическому CLI. Если изменение не вводит новое долговременное знание, новые инструкции вообще не нужны.
