"""Маскирование защищённых диапазонов и обратимое отображение координат движков.

Движок получает не исходный текст фрагмента, а его «вид»: защищённые вставки заменены
нейтральной заглушкой или своим буквальным значением (escape, сущность, разметка), CR
нормализован, соседние строки одного абзаца соединены переводом строки. Каждый символ вида
принадлежит сегменту с известным диапазоном исходника; позиция внутри замаскированного
сегмента не отображается и не угадывается.
"""

from __future__ import annotations

import html
import re
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

from language_review.extract_models import ProtectedSpan

TECHNICAL_REASON = "технический токен"
LINE_JOIN_REASON = "перенос строки между фрагментами"
CR_REASON = "символ CR"
PLAIN_PLACEHOLDER = "X"
LLM_PLACEHOLDER = re.compile(r"⟦\d+⟧")
LLM_PLACEHOLDER_CHARS = "⟦⟧"
MARKUP_REASONS = {
    "маркер выделения Markdown",
    "маркер синтаксиса ссылки",
    "адрес ссылки",
    "метка ссылки",
    "HTML-разметка или автоссылка",
    "маркер ссылки RST",
    "цель ссылки RST",
    "маркер роли RST",
    "маркер ссылки AsciiDoc",
    "цель ссылки AsciiDoc",
    "якорь AsciiDoc",
}
ESCAPE_REASONS = {
    "escape-последовательность",
    "escape sequence",
    "escape-последовательность YAML",
    "escape-последовательность TOML",
    "escape-последовательность PowerShell",
    "экранированная скобка",
    "экранированный %",
    "кавычка PowerShell",
    "кавычка C# verbatim",
    "экранирование Markdown",
}
_SIMPLE_ESCAPES = {
    "{{": "{",
    "}}": "}",
    "%%": "%",
    "''": "'",
    '""': '"',
    "\\n": "\n",
    "\\t": "\t",
    "\\r": "",
    "\\\\": "\\",
    '\\"': '"',
    "\\'": "'",
    "\\/": "/",
    "\\ ": " ",
    "`n": "\n",
    "`t": "\t",
    "`r": "",
    '`"': '"',
    "`'": "'",
    "``": "`",
    "`$": "$",
}
# Код и машинные значения внутри прозы, которые extract не обязан защищать: квалифицированные
# имена и вызовы, snake_case, camelCase/PascalCase с внутренней заглавной, флаги CLI, имена
# файлов и URL. LanguageTool иначе предлагает, например, «id. ToString()».
TECHNICAL_TOKEN = re.compile(
    r"""
    https?://[^\s<>"'`]+[^\s<>"'`.,;:!?)]
    | (?<![\w.])(?:
        [A-Za-z_][A-Za-z0-9_]*(?:(?:\.|::|->)[A-Za-z_][A-Za-z0-9_]*)+(?:\([^()\s]*\))?
      | [A-Za-z_][A-Za-z0-9_]*\([^()\s]*\)
      | [A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+
      | [a-z]+[A-Z][A-Za-z0-9]*
      | [A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*
      | --?[A-Za-z][A-Za-z0-9-]*
      | \\[A-Za-z]+
      | [\w-]+(?:\.[\w-]+)*\.(?:rs|cs|cpp|cc|cxx|c|h|hpp|hh|hxx|ps1|psm1|psd1|md|txt|rst|adoc
          |json|ya?ml|toml|xml|ini|cfg|conf|py|js|ts|sh|exe|dll|so|lock|log|csv|html?)
    )(?![\w])
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Segment:
    """Непрерывный участок вида; `source_*` — смещения символов исходного документа."""

    source_start: int
    source_end: int
    engine_start: int
    engine_end: int
    masked: bool
    reason: str | None = None
    placeholder: bool = False


@dataclass(frozen=True)
class MappedEdit:
    engine_start: int
    engine_end: int
    source_start: int
    source_end: int
    text: str


class Ambiguous:
    """Позиция вида соответствует нескольким позициям исходника."""


AMBIGUOUS = Ambiguous()


@dataclass(frozen=True)
class EngineView:
    text: str
    segments: tuple[Segment, ...]

    def _source_candidates(self, position: int) -> list[int] | None:
        """Все позиции исходника для границы вида; None — граница внутри маски."""
        if position < 0 or position > len(self.text):
            return None
        candidates: list[int] = []
        starts = [segment.engine_start for segment in self.segments]
        index = max(0, bisect_right(starts, position) - 1)
        while index > 0 and self.segments[index - 1].engine_end >= position:
            index -= 1
        for segment in self.segments[index:]:
            if segment.engine_start > position:
                break
            if position > segment.engine_end:
                continue
            if not segment.masked:
                candidates.append(segment.source_start + position - segment.engine_start)
            elif position == segment.engine_start:
                candidates.append(segment.source_start)
            elif position == segment.engine_end:
                candidates.append(segment.source_end)
            else:
                return None
        return sorted(set(candidates))

    def source_position(self, position: int, side: str) -> int | Ambiguous | None:
        candidates = self._source_candidates(position)
        if not candidates:
            return None
        if side == "start":
            return candidates[-1]
        if side == "end":
            return candidates[0]
        return candidates[0] if len(candidates) == 1 else AMBIGUOUS

    def source_range(self, start: int, end: int) -> tuple[int, int] | None:
        """Диапазон исходника для непустого диапазона вида или None, если граница в маске."""
        if not 0 <= start < end <= len(self.text):
            return None
        source_start = self.source_position(start, "start")
        source_end = self.source_position(end, "end")
        if not isinstance(source_start, int) or not isinstance(source_end, int):
            return None
        if source_start >= source_end:
            return None
        return source_start, source_end

    def overlaps_mask(self, start: int, end: int) -> bool:
        for segment in self.segments:
            if not segment.masked:
                continue
            if segment.engine_start == segment.engine_end:
                if start < segment.engine_start < end:
                    return True
            elif segment.engine_start < end and segment.engine_end > start:
                return True
        return False

    def only_masked(self, start: int, end: int) -> bool:
        """Весь диапазон состоит из замаскированных символов (без прозы)."""
        for segment in self.segments:
            if segment.masked or segment.engine_start == segment.engine_end:
                continue
            if segment.engine_start < end and segment.engine_end > start:
                return False
        return True

    def utf16_index(self, offset: int) -> int | None:
        """Перевести смещение UTF-16 в индекс code point; None — середина суррогатной пары."""
        units = 0
        for index, character in enumerate(self.text):
            if units == offset:
                return index
            units += 2 if ord(character) > 0xFFFF else 1
            if units > offset:
                return None
        return len(self.text) if units == offset else None

    def map_edit(self, start: int, end: int, replacement: str) -> MappedEdit | str:
        """Минимальная правка исходника для замены диапазона вида; иначе код отказа."""
        original = self.text[start:end]
        if replacement == original:
            return "replacement_identical"
        prefix = 0
        limit = min(len(original), len(replacement))
        while prefix < limit and original[prefix] == replacement[prefix]:
            prefix += 1
        suffix = 0
        while (
            suffix < limit - prefix
            and original[len(original) - 1 - suffix] == replacement[len(replacement) - 1 - suffix]
        ):
            suffix += 1
        edit_start, edit_end = start + prefix, end - suffix
        if edit_start == edit_end and isinstance(
            self.source_position(edit_start, "point"), Ambiguous
        ):
            # Вставка рядом с пустой маской (например, `**`): внутри или снаружи разметки —
            # исходник не определяет; расширение до слова скрыло бы этот выбор.
            return "ambiguous_insertion_position"
        # Расширить правку до границ слова внутри исходного диапазона: «пошол» → «пошёл»,
        # а не «о» → «ё». Расширение не пересекает маски.
        while (
            edit_start > start
            and self.text[edit_start - 1].isalnum()
            and not self.overlaps_mask(edit_start - 1, edit_end)
        ):
            edit_start -= 1
        while (
            edit_end < end
            and self.text[edit_end].isalnum()
            and not self.overlaps_mask(edit_start, edit_end + 1)
        ):
            edit_end += 1
        inserted = replacement[edit_start - start : len(replacement) - (end - edit_end)]
        if LLM_PLACEHOLDER.search(inserted) or any(
            character in inserted for character in LLM_PLACEHOLDER_CHARS
        ):
            return "replacement_changes_protected_text"
        if self.overlaps_mask(edit_start, edit_end):
            return "replacement_overlaps_protected_range"
        if edit_start == edit_end:
            point = self.source_position(edit_start, "point")
            if point is None:
                return "replacement_overlaps_protected_range"
            if isinstance(point, Ambiguous):
                return "ambiguous_insertion_position"
            return MappedEdit(edit_start, edit_end, point, point, inserted)
        mapped = self.source_range(edit_start, edit_end)
        if mapped is None:
            return "replacement_overlaps_protected_range"
        return MappedEdit(edit_start, edit_end, mapped[0], mapped[1], inserted)


@dataclass(frozen=True)
class ViewFragment:
    """Фрагмент extract для построения вида: точная цитата и защищённые диапазоны."""

    start_char: int
    end_char: int
    text: str
    protected: tuple[ProtectedSpan, ...]


def decode_literal(text: str) -> str | None:
    """Буквальное значение escape-последовательности или None, если его нельзя показать."""
    if text in _SIMPLE_ESCAPES:
        return _SIMPLE_ESCAPES[text]
    if len(text) == 2 and text[0] == "\\" and not text[1].isalnum():
        return text[1]
    match = re.fullmatch(r"\\(?:x([0-9A-Fa-f]{2})|u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8}))", text)
    if match is not None:
        value = int(next(group for group in match.groups() if group), 16)
        try:
            character = chr(value)
        except ValueError:
            return None
        return character if character.isprintable() else None
    if re.fullmatch(r"\\\r?\n[ \t]*", text):
        return ""
    return None


def interpret_protected(text: str, reason: str) -> str | None:
    """Чем показать защищённую вставку движку; None — нейтральной заглушкой."""
    reasons = set(reason.split("; "))
    if reasons <= MARKUP_REASONS:
        if text.startswith(("<http", "<mailto")):
            return None
        return ""
    if reasons <= ESCAPE_REASONS:
        return decode_literal(text)
    if reasons == {"ссылка на сущность"}:
        decoded = html.unescape(text)
        if decoded == text or not decoded.isprintable():
            return None
        return decoded
    return None


def _technical_spans(fragment: ViewFragment) -> list[ProtectedSpan]:
    return [
        ProtectedSpan(
            fragment.start_char + match.start(),
            fragment.start_char + match.end(),
            TECHNICAL_REASON,
        )
        for match in TECHNICAL_TOKEN.finditer(fragment.text)
    ]


def _merged_masks(fragment: ViewFragment) -> list[ProtectedSpan]:
    spans = sorted(
        [*fragment.protected, *_technical_spans(fragment)],
        key=lambda span: (span.start_char, span.end_char),
    )
    merged: list[ProtectedSpan] = []
    for span in spans:
        if span.start_char >= span.end_char:
            continue
        if merged and span.start_char < merged[-1].end_char:
            previous = merged[-1]
            reasons = sorted(set(previous.reason.split("; ")) | set(span.reason.split("; ")))
            merged[-1] = ProtectedSpan(
                previous.start_char, max(previous.end_char, span.end_char), "; ".join(reasons)
            )
        else:
            merged.append(span)
    return merged


class _Builder:
    def __init__(self, style: str) -> None:
        self.style = style
        self.parts: list[str] = []
        self.segments: list[Segment] = []
        self.length = 0
        self.placeholders = 0

    def _append(
        self,
        source_start: int,
        source_end: int,
        engine_text: str,
        *,
        masked: bool,
        reason: str | None = None,
        placeholder: bool = False,
    ) -> None:
        previous = self.segments[-1] if self.segments else None
        if (
            not masked
            and previous is not None
            and not previous.masked
            and previous.source_end == source_start
        ):
            self.segments[-1] = Segment(
                previous.source_start,
                source_end,
                previous.engine_start,
                previous.engine_end + len(engine_text),
                False,
            )
        else:
            self.segments.append(
                Segment(
                    source_start,
                    source_end,
                    self.length,
                    self.length + len(engine_text),
                    masked,
                    reason,
                    placeholder,
                )
            )
        self.parts.append(engine_text)
        self.length += len(engine_text)

    def text(self, start: int, value: str) -> None:
        cursor = 0
        while cursor < len(value):
            carriage = value.find("\r", cursor)
            if carriage < 0:
                self._append(start + cursor, start + len(value), value[cursor:], masked=False)
                return
            if carriage > cursor:
                self._append(start + cursor, start + carriage, value[cursor:carriage], masked=False)
            # CRLF превращается в LF, одиночный CR — в LF: CR не видим движку, но обратим.
            lone = carriage + 1 >= len(value) or value[carriage + 1] != "\n"
            self._append(
                start + carriage,
                start + carriage + 1,
                "\n" if lone else "",
                masked=True,
                reason=CR_REASON,
            )
            cursor = carriage + 1

    def mask(self, start: int, end: int, original: str, reason: str) -> None:
        literal = interpret_protected(original, reason)
        if literal is not None:
            self._append(start, end, literal, masked=True, reason=reason)
            return
        self.placeholders += 1
        token = PLAIN_PLACEHOLDER if self.style == "plain" else f"⟦{self.placeholders}⟧"
        self._append(start, end, token, masked=True, reason=reason, placeholder=True)

    def join(self, start: int, end: int) -> None:
        self._append(start, end, "\n", masked=True, reason=LINE_JOIN_REASON)

    def view(self) -> EngineView:
        return EngineView("".join(self.parts), tuple(self.segments))


def build_view(fragments: Sequence[ViewFragment], style: str) -> EngineView:
    """Построить вид для движка: `plain` — заглушка `X`, `llm` — нумерованные `⟦n⟧`."""
    if style not in {"plain", "llm"}:
        raise ValueError(f"Неизвестный стиль заглушек: {style}")
    builder = _Builder(style)
    for index, fragment in enumerate(fragments):
        if index:
            builder.join(fragments[index - 1].end_char, fragment.start_char)
        cursor = fragment.start_char
        for span in _merged_masks(fragment):
            start = max(span.start_char, fragment.start_char)
            end = min(span.end_char, fragment.end_char)
            if start >= end or start < cursor:
                continue
            if start > cursor:
                builder.text(
                    cursor,
                    fragment.text[cursor - fragment.start_char : start - fragment.start_char],
                )
            builder.mask(
                start,
                end,
                fragment.text[start - fragment.start_char : end - fragment.start_char],
                span.reason,
            )
            cursor = end
        if cursor < fragment.end_char:
            builder.text(cursor, fragment.text[cursor - fragment.start_char :])
    return builder.view()
