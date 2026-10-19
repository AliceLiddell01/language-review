# Синтетический контроль оценщика

Оценка: PARTIAL · synthetic_contract_check
Split: dev; корпус: ecea64c50b5819d4ae4904c01f508e9e89f81ca6f54f8355ffde4b00c3196b03
Случаев: 15; оценено применимых: 11; неполных: 3; не оценено: 1
Уровень | TP | FP | FN | Precision | Recall | F1 | F0.5
--- | --- | --- | --- | --- | --- | --- | ---
detection | 2 | 0 | 7 | 1.0 | 0.2222222222222222 | 0.36363636363636365 | 0.5882352941176471
pipeline_detection | 2 | 0 | 9 | 1.0 | 0.18181818181818182 | 0.3076923076923077 | 0.5263157894736842
correction | 1 | 0 | 10 | 1.0 | 0.09090909090909091 | 0.16666666666666666 | 0.3333333333333333
TN чистых случаев: 3; FP/1000 оценённых применимых случаев: 0.0
Статусы разметки: {"independently_verified": 14, "disputed": 1}
Разрез format: n — число случаев, не оценка репрезентативности.
Группа | n | TP | FP | FN
--- | --- | --- | --- | ---
"cpp" | 1 | 0 | 0 | 1
"cs" | 1 | 0 | 0 | 1
"md" | 6 | 1 | 0 | 4
"ps1" | 2 | 0 | 0 | 1
"rs" | 2 | 0 | 0 | 2
"txt" | 3 | 1 | 0 | 0
Разрез context: n — число случаев, не оценка репрезентативности.
Группа | n | TP | FP | FN
--- | --- | --- | --- | ---
"comment" | 4 | 0 | 0 | 4
"documentation" | 8 | 2 | 0 | 4
"log" | 1 | 0 | 0 | 0
"message" | 2 | 0 | 0 | 1
Разрез data_kind: n — число случаев, не оценка репрезентативности.
Группа | n | TP | FP | FN
--- | --- | --- | --- | ---
"synthetic" | 15 | 2 | 0 | 9
Разрез category: n — число случаев, не оценка репрезентативности.
Группа | n | TP | FP | FN
--- | --- | --- | --- | ---
"bureaucratese" | 1 | 1 | 0 | 0
"grammar" | 4 | 0 | 0 | 3
"punctuation" | 1 | 0 | 0 | 1
"spelling" | 3 | 0 | 0 | 4
"style" | 3 | 0 | 0 | 0
"terminology" | 1 | 1 | 0 | 0
"untranslated_english" | 2 | 0 | 0 | 1
Движок vale: TP=2 FP=0 FN=9; вклад {"unique_final_tp": [["style", "gold-1"], ["terminology", "gold-1"]], "shared_final_tp": []}
"spelling": evaluated; TP=[]; FN=['gold-1']; причины=[]
"agreement": evaluated; TP=[]; FN=['gold-1']; причины=[]
"punctuation": evaluated; TP=[]; FN=['gold-1']; причины=[]
"repetition": incomplete; TP=[]; FN=['gold-1']; причины=['before:extract_uncovered', 'before:extraction_partial', 'document:partial']
"style": evaluated; TP=['gold-1']; FN=[]; причины=[]
"terminology": evaluated; TP=['gold-1']; FN=[]; причины=[]
"english": incomplete; TP=[]; FN=['gold-1']; причины=['before:no_applicable_checked_engine']
"short-english": incomplete; TP=[]; FN=[]; причины=['before:no_applicable_checked_engine']
"technical": evaluated; TP=[]; FN=[]; причины=[]
"log": evaluated; TP=[]; FN=[]; причины=[]
"unicode": evaluated; TP=[]; FN=['gold-1']; причины=[]
"alternatives": evaluated; TP=[]; FN=['gold-1']; причины=[]
"multiple": evaluated; TP=[]; FN=['gold-1', 'gold-2']; причины=[]
"clean": evaluated; TP=[]; FN=[]; причины=[]
"disputed": not_evaluated; TP=[]; FN=[]; причины=['annotation:not_verified_exhaustive_applicable']
Доступные затраты: {"wall_clock_ms": null, "engines": [{"name": "vale", "elapsed_ms": 653.7, "statistics": {"filtered": 0, "findings": 2, "fragments_checked": 12, "rejected": 0, "request_failures": 0, "requests": 12, "units_checked": 12, "units_failed": 0, "units_skipped": 0, "units_total": 12}, "tokens": {}}], "money": null, "resource_usage": null, "retries": null}
Ограничения: scan viewed/selected totals are absent in check 1.0; check sources are localized retained outputs, not all raw proposals; filtered/unlocalized hypotheses have no reliable source ranges and cannot earn TP; no independent semantic harm judge; risk reasons are diagnostics; small strata: inspect n; no population inference
