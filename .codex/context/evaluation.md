# Контракт независимой оценки

- `evaluation/` — отдельный оценщик сохранённых check/review; entry points:
  `language-review-eval`, `python -m language_review.evaluation`. Руководство и формулы:
  [docs/evaluation.md](../../docs/evaluation.md). Владельцы JSON-контрактов —
  `evaluation/schemas/`; check-схема упакована без изменения, её равенство проверяет тест.
- Корпус содержит неизменяемые байты, provenance/rights, split и состояния аннотаций.
  Реальные independently_verified метки требуют human/external_reference. Оценщик не
  превращает собственные или модельные гипотезы в истину. Синтетические, независимые
  и exploratory отчёты имеют разные report_kind.
- `import-source` использует pinned каталог `evaluation/sources.json`, проверяет все
  hashes/основания прав; upstream bytes/references/provenance остаются вне Git. Split
  каждой семьи задаётся каталогом. Пары и отсутствие исправления не подтверждают
  gold/clean; автоматические proposals только draft. Детали источников и форматов см.
  в [docs/evaluation.md](../../docs/evaluation.md).
- `reference-score` оценивает только точное строковое совпадение с pinned correction,
  отдельно от gold scoring. Альтернативы/конфликты candidate-правок неопределённы;
  нулевой знаменатель null. Ограничения протокола и источников описаны в
  [docs/evaluation.md](../../docs/evaluation.md).
- `annotation-export/import` — слепой JSONL/CSV round-trip с codepoint offsets,
  точными quote/hash/protected ranges и явным человеческим свидетельством. Verified
  требует exhaustive по указанным категориям; untouched/disputed не повышаются.
  Замороженный test не изменяется; agreement не назначает истину и не подтверждает личность.
- Matching строгий и взаимно однозначный; частичное пересечение не TP. Неоднозначность
  явная, correction отдельна от detection. Смысловой ущерб оценивается только независимыми
  judgments; машинные risk reasons не доказательство. Незапущенные/непокрытые случаи не TN.
- Офлайн-оценка детерминирована и не вызывает движки. Явный `run` запускает публичный check
  на временных байтах выбранного split; отчёты вне входов, метки не передаются анализаторам.
  Исходники и существующие артефакты не перезаписываются. Реальные данные/отчёты — вне Git.
- Сравнение допускает только одинаковые corpus/annotation/split/metric/scorer/protocol.
  Регрессионные ожидания относятся к сохранённой выдаче, а не к устойчивости живой LLM.
  Ограничения check 1.0 не компенсируются выдуманными scan/raw-output диагностиками.
