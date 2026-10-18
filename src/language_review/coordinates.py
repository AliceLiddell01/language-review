from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass


@dataclass(frozen=True)
class SourcePosition:
    byte_offset: int
    line: int
    column: int
    utf16_column: int

    def as_dict(self) -> dict[str, int]:
        return {
            "byte_offset": self.byte_offset,
            "line": self.line,
            "column": self.column,
            "utf16_column": self.utf16_column,
        }


@dataclass(frozen=True)
class MappedText:
    """Text whose characters map back to contiguous source character spans."""

    text: str
    source_spans: tuple[tuple[int, int], ...]

    def source_span(self, start: int, end: int) -> tuple[int, int] | None:
        if start < 0 or end > len(self.text) or start >= end:
            return None
        source_start, source_end = self.source_spans[start]
        previous_end = source_end
        for item_start, item_end in self.source_spans[start + 1 : end]:
            if item_start != previous_end:
                return None
            previous_end = item_end
        return source_start, previous_end


@dataclass(frozen=True)
class SourceMap:
    raw: bytes
    encoding: str
    bom: str | None
    text: str
    parser_bytes: bytes
    _source_byte_offsets: tuple[int, ...]
    _parser_byte_offsets: tuple[int, ...]
    _line_starts: tuple[int, ...]

    @classmethod
    def create(cls, raw: bytes, encoding: str, bom: str | None) -> SourceMap:
        bom_bytes = {
            "utf-8": b"\xef\xbb\xbf",
            "utf-16-le": b"\xff\xfe",
            "utf-16-be": b"\xfe\xff",
            "utf-32-le": b"\xff\xfe\x00\x00",
            "utf-32-be": b"\x00\x00\xfe\xff",
        }
        prefix = bom_bytes.get(bom or "", b"")
        if prefix and not raw.startswith(prefix):
            raise ValueError("BOM исходника не совпадает с метаданными scan.")
        text = raw[len(prefix) :].decode(encoding, errors="strict")

        source_offsets = [len(prefix)]
        parser_offsets = [0]
        for character in text:
            source_offsets.append(source_offsets[-1] + len(character.encode(encoding)))
            parser_offsets.append(parser_offsets[-1] + len(character.encode("utf-8")))

        line_starts = [0]
        index = 0
        while index < len(text):
            character = text[index]
            if character == "\r":
                index += 2 if index + 1 < len(text) and text[index + 1] == "\n" else 1
                line_starts.append(index)
                continue
            if character == "\n":
                line_starts.append(index + 1)
            index += 1

        return cls(
            raw=raw,
            encoding=encoding,
            bom=bom,
            text=text,
            parser_bytes=text.encode("utf-8"),
            _source_byte_offsets=tuple(source_offsets),
            _parser_byte_offsets=tuple(parser_offsets),
            _line_starts=tuple(line_starts),
        )

    def parser_byte_to_char(self, offset: int) -> int:
        index = bisect_left(self._parser_byte_offsets, offset)
        if index == len(self._parser_byte_offsets) or self._parser_byte_offsets[index] != offset:
            raise ValueError("Смещение Tree-sitter не совпадает с границей символа.")
        return index

    def position(self, char_offset: int) -> SourcePosition:
        if char_offset < 0 or char_offset > len(self.text):
            raise ValueError("Смещение символа вне исходного файла.")
        line_index = bisect_right(self._line_starts, char_offset) - 1
        line_start = self._line_starts[line_index]
        prefix = self.text[line_start:char_offset]
        return SourcePosition(
            byte_offset=self._source_byte_offsets[char_offset],
            line=line_index + 1,
            column=len(prefix) + 1,
            utf16_column=len(prefix.encode("utf-16-le")) // 2 + 1,
        )

    def range_dict(self, start: int, end: int) -> dict[str, dict[str, int]]:
        if start > end:
            raise ValueError("Конец диапазона предшествует началу.")
        return {"start": self.position(start).as_dict(), "end": self.position(end).as_dict()}

    def identity_view(self, start: int = 0, end: int | None = None) -> MappedText:
        limit = len(self.text) if end is None else end
        return MappedText(
            text=self.text[start:limit],
            source_spans=tuple((index, index + 1) for index in range(start, limit)),
        )

    def source_text(self, start: int, end: int) -> str:
        return self.text[start:end]

    def line_bounds(self, line_number: int) -> tuple[int, int, int]:
        """Return zero-based line start, content end, and end including its newline."""
        if line_number < 0 or line_number >= len(self._line_starts):
            raise ValueError("Номер строки вне исходного файла.")
        start = self._line_starts[line_number]
        if line_number + 1 < len(self._line_starts):
            end_with_newline = self._line_starts[line_number + 1]
            end = end_with_newline
            if self.text[end - 1 : end] == "\n":
                end -= 1
                if end > start and self.text[end - 1 : end] == "\r":
                    end -= 1
            elif self.text[end - 1 : end] == "\r":
                end -= 1
        else:
            end = len(self.text)
            end_with_newline = end
        return start, end, end_with_newline
