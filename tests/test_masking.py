from __future__ import annotations

from pathlib import Path

import pytest

from language_review.check import build_units, source_position
from language_review.config import CheckSettings, load_config
from language_review.coordinates import SourceMap, position_within
from language_review.extraction import extract_paths
from language_review.masking import AMBIGUOUS, ViewFragment, build_view, decode_literal


def _units(path: Path) -> list:
    result = extract_paths([str(path)], load_config())
    return build_units(result.documents, [CheckSettings() for _ in result.documents])


@pytest.mark.parametrize(
    ("raw", "encoding", "bom"),
    [
        ("Строка\r\nвторая\tстрока 😀\rтретья\nконец".encode(), "utf-8", None),
        (b"\xef\xbb\xbf" + "Ё😀\r\nж".encode(), "utf-8", "utf-8"),
        ("Привет\r\nмир".encode("windows-1251"), "windows-1251", None),
        (b"\xff\xfe" + "Да😀\nнет".encode("utf-16-le"), "utf-16-le", "utf-16-le"),
    ],
)
def test_position_within_matches_source_map_for_every_offset(
    raw: bytes, encoding: str, bom: str | None
) -> None:
    source = SourceMap.create(raw, encoding, bom)
    start, end = 0, len(source.text)
    anchor, finish = source.position(start).as_dict(), source.position(end).as_dict()
    for offset in range(end + 1):
        assert position_within(anchor, finish, source.text, encoding, offset) == (
            source.position(offset).as_dict()
        ), offset


def test_languagetool_utf16_offsets_after_non_bmp_characters() -> None:
    fragment = ViewFragment(0, 13, "😀 и праверить", ())
    view = build_view([fragment], "plain")

    # «праверить» начинается с кодовой точки 4, но в UTF-16 — с 5: эмодзи занимает пару.
    assert view.utf16_index(5) == 4
    assert view.utf16_index(1) is None
    assert view.source_range(4, 13) == (4, 13)


def test_technical_tokens_placeholders_and_insertion_after_mask(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    source = tmp_path / "note.txt"
    source.write_text("Вызовите id.ToString() чтобы получить строку.\n", encoding="utf-8")

    unit = _units(source)[0]
    view = unit.views["plain"]

    assert view.text == "Вызовите X чтобы получить строку."
    assert unit.views["llm"].text == "Вызовите ⟦1⟧ чтобы получить строку."
    # Так отвечает LanguageTool: замена включает заглушку, а правка — только запятую.
    edit = view.map_edit(9, 25, "X, чтобы получить")
    assert not isinstance(edit, str)
    assert (edit.source_start, edit.source_end, edit.text) == (22, 22, ",")
    assert source_position(unit, edit.source_start)["column"] == 23
    # Правка внутри технического токена (как «id. ToString()») недопустима.
    assert view.map_edit(9, 10, "id. ToString()") == "replacement_overlaps_protected_range"
    assert view.only_masked(9, 10)


def test_literal_interpretation_of_escapes_entities_and_markdown(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    source = tmp_path / "doc.md"
    source.write_text("Это **важно** и &laquo;точно&raquo; для {Path}.\n", encoding="utf-8")

    unit = _units(source)[0]
    view = unit.views["plain"]

    assert view.text == "Это важно и «точно» для X."
    end_of_word = view.text.index("важно") + len("важно")
    # После «важно» стоит пустая маска `**`: место вставки в исходнике неоднозначно.
    assert view.source_position(end_of_word, "point") is AMBIGUOUS
    assert view.map_edit(end_of_word - 5, end_of_word, "важно,") == "ambiguous_insertion_position"
    assert decode_literal("\\n") == "\n"
    assert decode_literal("{{") == "{"
    assert decode_literal("\\u0041") == "A"
    assert decode_literal("\\x07") is None


def test_crlf_and_line_join_are_reversible(tmp_path: Path, isolated_appdata: Path) -> None:
    source = tmp_path / "doc.md"
    source.write_bytes(
        b"\xef\xbb\xbf" + "Первая строка абзаца\r\nпродолжение без ошыбок.\r\n".encode()
    )

    unit = _units(source)[0]
    view = unit.views["plain"]

    assert len(unit.fragments) == 2
    assert view.text == "Первая строка абзаца\nпродолжение без ошыбок."
    start = view.text.index("ошыбок")
    mapped = view.source_range(start, start + len("ошыбок"))
    assert mapped is not None
    position = source_position(unit, mapped[0])
    assert (position["line"], position["column"]) == (2, 17)
    # BOM входит в byte_offset: 3 байта BOM + первая строка + CRLF + «продолжение без ».
    assert position["byte_offset"] == 3 + len("Первая строка абзаца\r\nпродолжение без ".encode())
    # Диапазон, пересекающий соединение строк, отображается на оба фрагмента.
    joined = view.source_range(view.text.index("абзаца"), view.text.index("без"))
    assert joined is not None
    assert view.map_edit(view.text.index("абзаца"), view.text.index("без"), "абзаца, ") == (
        "replacement_overlaps_protected_range"
    )
