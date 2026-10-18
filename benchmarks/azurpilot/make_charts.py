"""Диаграммы PR из сохранённых агрегатов results-after.json и results-before.json (SVG, stdlib).

    uv run --locked python benchmarks/azurpilot/make_charts.py

PNG для описания PR строится из SVG браузером (Edge/Chrome `--headless --screenshot`).
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).parent
OUT = HERE / "charts"
FONT = "font-family='Segoe UI, Arial, sans-serif'"
COLORS = {"ok": "#2da44e", "skip": "#8c959f", "fail": "#cf222e", "fp": "#cf222e", "tp": "#2da44e"}


def load(name: str) -> dict:
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def svg(width: int, height: int, body: list[str], title: str) -> str:
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
        f"viewBox='0 0 {width} {height}'><rect width='100%' height='100%' fill='#ffffff'/>"
        f"<text x='16' y='28' {FONT} font-size='17' font-weight='600' fill='#1f2328'>"
        f"{escape(title)}</text>" + "".join(body) + "</svg>\n"
    )


def stacked(
    rows: list[tuple[str, list[tuple[str, int, str]]]], title: str, legend: bool = True
) -> str:
    """Горизонтальные составные столбцы: rows = [(подпись, [(имя, значение, цвет)])]."""
    left, bar_width, row_height = 250, 520, 34
    top = 60
    height = top + row_height * len(rows) + 50
    maximum = max(sum(v for _, v, _ in parts) for _, parts in rows) or 1
    body: list[str] = []
    for index, (label, parts) in enumerate(rows):
        y = top + index * row_height
        body.append(
            f"<text x='{left - 10}' y='{y + 17}' {FONT} font-size='13' text-anchor='end' "
            f"fill='#1f2328'>{escape(label)}</text>"
        )
        x = float(left)
        for _name, value, color in parts:
            if value <= 0:
                continue
            w = bar_width * value / maximum
            body.append(f"<rect x='{x:.1f}' y='{y}' width='{w:.1f}' height='22' fill='{color}'/>")
            if w > 28:
                body.append(
                    f"<text x='{x + w / 2:.1f}' y='{y + 16}' {FONT} font-size='12' "
                    f"text-anchor='middle' fill='#ffffff'>{value}</text>"
                )
            x += w
        body.append(
            f"<text x='{x + 6:.1f}' y='{y + 16}' {FONT} font-size='12' fill='#57606a'>"
            f"{sum(v for _, v, _ in parts)}</text>"
        )
    if legend:
        names: dict[str, str] = {}
        for _, parts in rows:
            for name, _, color in parts:
                names[name] = color
        x = left
        for name, color in names.items():
            body.append(f"<rect x='{x}' y='{height - 30}' width='12' height='12' fill='{color}'/>")
            body.append(
                f"<text x='{x + 18}' y='{height - 20}' {FONT} font-size='12' fill='#1f2328'>"
                f"{escape(name)}</text>"
            )
            x += 28 + 7 * len(name)
    return svg(left + bar_width + 80, height, body, title)


def main() -> None:
    after, before = load("results-after.json"), load("results-before.json")
    OUT.mkdir(exist_ok=True)

    # 1. Охват единиц образца по движкам (после) и LLM до исправления.
    rows = []
    for label, result in (("после", after), ("до", before)):
        sample = result["sample_all_engines"]
        coverage = sample["coverage"]
        for engine in sample["engines"]:
            stats = engine["statistics"]
            if label == "до" and engine["name"] != "llm":
                continue
            applicable = stats["units_total"]
            not_applicable = coverage["units"] - applicable
            rows.append(
                (
                    f"{engine['name']} ({label})",
                    [
                        ("проверено", stats["units_checked"], COLORS["ok"]),
                        (
                            "сбой/пропуск",
                            stats["units_failed"] + stats["units_skipped"],
                            COLORS["fail"],
                        ),
                        ("неприменимо (нет кириллицы)", not_applicable, COLORS["skip"]),
                    ],
                )
            )
    (OUT / "coverage.svg").write_text(
        stacked(rows, "Охват единиц образца (379 единиц, 12 файлов)"), encoding="utf-8"
    )

    # 2. Разметка находок в проверенном эталоном объёме (после).
    score = after["sample_all_engines"]["score"]
    rows = []
    for name in ("languagetool", "vale", "llm", "union"):
        item = score[name]
        rows.append(
            (
                "объединение" if name == "union" else name,
                [
                    ("TP", item["tp"], COLORS["tp"]),
                    ("спорные", item["debatable_hits"], "#bf8700"),
                    ("FP", item["fp"], COLORS["fp"]),
                ],
            )
        )
    (OUT / "labeled-findings.svg").write_text(
        stacked(rows, "Находки в размеченном объёме: TP / спорные / FP (эталон: 1 дефект)"),
        encoding="utf-8",
    )

    # 3. Время движков (с), после и до.
    rows = []
    for label, result in (("после", after), ("до", before)):
        for engine in result["sample_all_engines"]["engines"]:
            rows.append(
                (
                    f"{engine['name']} ({label})",
                    [("время, с", round(engine["elapsed_s"]), "#0969da")],
                )
            )
    (OUT / "time.svg").write_text(
        stacked(rows, "Время движков на образце, с (warm LanguageTool/llama.cpp)", legend=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
