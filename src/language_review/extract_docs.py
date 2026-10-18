from __future__ import annotations

import re
from collections.abc import Iterable

from markdown_it import MarkdownIt
from markdown_it.token import Token

from language_review.coordinates import MappedText, SourceMap
from language_review.extract_code import _inline_protection, _is_machine_literal, _merge_spans
from language_review.extract_models import FragmentDraft, ProtectedSpan


def _mapped_inline_lines(
    source: SourceMap, token: Token, line_cursors: dict[int, int]
) -> list[tuple[int, MappedText]] | None:
    if token.map is None:
        return None
    content_lines = token.content.split("\n")
    first_line, last_line = token.map
    if len(content_lines) != last_line - first_line:
        return None
    output: list[tuple[int, MappedText]] = []
    for offset, content_line in enumerate(content_lines):
        source_line = first_line + offset
        line_start, line_end, line_with_newline = source.line_bounds(source_line)
        if not content_line:
            output.append((source_line, MappedText("", ())))
            continue
        cursor = max(line_start, line_cursors.get(source_line, line_start))
        match = source.text.find(content_line, cursor, line_end)
        if match < 0:
            return None
        match_end = match + len(content_line)
        line_cursors[source_line] = match_end
        spans = [(index, index + 1) for index in range(match, match_end)]
        output.append((source_line, MappedText(content_line, tuple(spans))))
        if offset + 1 < len(content_lines) and line_with_newline > line_end:
            line_cursors.setdefault(source_line + 1, line_start)
    return output


def _markdown_protected(text: str, source_start: int) -> tuple[ProtectedSpan, ...]:
    spans = list(_inline_protection(text, source_start, "markdown"))
    patterns = (
        (r"!\[[^\]]*\]\([^)]*\)", "image syntax and destination"),
        (r"\[|\]", "link syntax marker"),
        (r"\]\([^)]*\)", "link destination"),
        (r"(?<=\])\[[^\]]*\]", "reference destination"),
        (r"<[^>]+>", "HTML markup or autolink"),
        (r"(?<!\*)\*{1,3}|\*{1,3}(?!\*)|_{1,3}|~{2}", "Markdown emphasis marker"),
    )
    for pattern, reason in patterns:
        spans.extend(
            ProtectedSpan(source_start + match.start(), source_start + match.end(), reason)
            for match in re.finditer(pattern, text)
        )
    spans.extend(
        ProtectedSpan(source_start + match.start(), source_start + match.end(), "Markdown escape")
        for match in re.finditer(r"\\[^\w\s]", text)
    )
    return _merge_spans(spans)


def _visible_line(text: str, source_start: int) -> str:
    visible = list(text)
    for span in _markdown_protected(text, source_start):
        start = span.start_char - source_start
        end = span.end_char - source_start
        visible[start:end] = " " * (end - start)
    return "".join(visible).strip()


def _front_matter_lines(text: str) -> set[int]:
    lines = text.splitlines()
    if not lines or lines[0].strip() not in {"---", "+++"}:
        return set()
    marker = lines[0].strip()
    for index in range(1, len(lines)):
        if lines[index].strip() in {marker, "..."}:
            return set(range(index + 1))
    return set()


def _block_context(stack: Iterable[Token]) -> str:
    names = []
    for token in stack:
        if token.type == "heading_open":
            names.append(token.tag)
        elif token.type in {"bullet_list_open", "ordered_list_open"}:
            names.append("list")
        elif token.type == "th_open":
            names.append("table_header")
        elif token.type == "td_open":
            names.append("table_cell")
        elif token.type == "blockquote_open":
            names.append("blockquote")
    return "markdown:" + ("/".join(names) if names else "prose")


def extract_markdown(source: SourceMap) -> tuple[list[FragmentDraft], list[str]]:
    parser = MarkdownIt("commonmark").enable("table")
    tokens = parser.parse(source.text)
    excluded_lines = _front_matter_lines(source.text)
    stack: list[Token] = []
    line_cursors: dict[int, int] = {}
    fragments: list[FragmentDraft] = []
    diagnostics: list[str] = []
    for token in tokens:
        if token.type.endswith("_close"):
            if stack:
                stack.pop()
            continue
        if token.type.endswith("_open"):
            stack.append(token)
            continue
        if token.type != "inline" or token.map is None:
            continue
        first_line, last_line = token.map
        if any(line in excluded_lines for line in range(first_line, last_line)):
            continue
        if any(item.type == "blockquote_open" for item in stack):
            continue
        mapped_lines = _mapped_inline_lines(source, token, line_cursors)
        if mapped_lines is None:
            diagnostics.append("ambiguous_markdown_source_mapping")
            continue
        has_readable_text = any(
            child.type == "text" and child.content.strip() for child in token.children or ()
        )
        if not has_readable_text:
            continue
        context = _block_context(stack)
        for _line_number, view in mapped_lines:
            if not view.text.strip() or not view.source_spans:
                continue
            mapped_span = view.source_span(0, len(view.text))
            if mapped_span is None:
                diagnostics.append("non_contiguous_markdown_source_mapping")
                continue
            source_start, source_end = mapped_span
            raw_text = source.text[source_start:source_end]
            visible = _visible_line(raw_text, source_start)
            if not visible or _is_machine_literal(visible):
                continue
            fragments.append(
                FragmentDraft(
                    "markdown_prose",
                    context,
                    raw_text,
                    source_start,
                    source_end,
                    _markdown_protected(raw_text, source_start),
                )
            )
    fragments.sort(key=lambda item: (item.start_char, item.end_char, item.context))
    return fragments, sorted(set(diagnostics))


_RST_DIRECTIVE = re.compile(r"^\s*\.\.\s+(?:[A-Za-z][\w-]*::|_[^:]+:|\[[^]]+\])")
_RST_LITERAL = re.compile(r"^\s*\.\.\s+(?:code-block|sourcecode|literalinclude)::")
_ADOC_ATTRIBUTE = re.compile(r"^\s*:[A-Za-z0-9_-]+:")
_ADOC_BLOCK = {"----", "....", "++++", "////", "____"}


def _markup_protected(text: str, start: int, dialect: str) -> tuple[ProtectedSpan, ...]:
    spans = list(_inline_protection(text, start, "comment"))
    patterns: tuple[tuple[str, str], ...]
    if dialect == "rst":
        patterns = ((r"``[^`]*``", "reStructuredText inline literal"),)
        for match in re.finditer(r"`([^`<>]+)\s*(<[^>]+>)`_?", text):
            spans.extend(
                (
                    ProtectedSpan(start + match.start(), start + match.start(1), "RST link marker"),
                    ProtectedSpan(start + match.start(2), start + match.end(2), "RST link target"),
                    ProtectedSpan(start + match.end(2), start + match.end(), "RST link marker"),
                )
            )
        for match in re.finditer(r":(?:[A-Za-z][\w-]*:)?`([^`]+)`", text):
            label_start, label_end = match.span(1)
            spans.extend(
                (
                    ProtectedSpan(start + match.start(), start + label_start, "RST role marker"),
                    ProtectedSpan(start + label_end, start + match.end(), "RST role marker"),
                )
            )
    else:
        patterns = (
            (r"`[^`]+`", "AsciiDoc monospace"),
            (r"\+[^+]+\+", "AsciiDoc literal"),
            (r"(?:https?://|mailto:)[^\s\])>]+", "URL"),
            (r"\[\[[^]]+\]\]|\[#[-\w.]+\]", "AsciiDoc anchor"),
        )
        for match in re.finditer(r"(?:link|xref):[^\[]+\[([^]]*)\]", text):
            label_start, label_end = match.span(1)
            spans.extend(
                (
                    ProtectedSpan(
                        start + match.start(), start + label_start, "AsciiDoc link target"
                    ),
                    ProtectedSpan(start + label_end, start + match.end(), "AsciiDoc link marker"),
                )
            )
    for pattern, reason in patterns:
        spans.extend(
            ProtectedSpan(start + match.start(), start + match.end(), reason)
            for match in re.finditer(pattern, text)
        )
    return _merge_spans(spans)


def extract_markup_text(source: SourceMap, dialect: str) -> tuple[list[FragmentDraft], list[str]]:
    fragments: list[FragmentDraft] = []
    diagnostics: list[str] = []
    in_block = False
    block_marker: str | None = None
    literal_indent: int | None = None
    lines = source.text.splitlines(keepends=True)
    char_start = 0
    for full_line in lines:
        content = full_line.rstrip("\r\n")
        if dialect == "adoc" and content.strip() in _ADOC_BLOCK:
            marker = content.strip()
            if not in_block:
                in_block, block_marker = True, marker
            elif marker == block_marker:
                in_block, block_marker = False, None
            char_start += len(full_line)
            continue
        if in_block:
            char_start += len(full_line)
            continue
        if dialect == "rst":
            indentation = len(content) - len(content.lstrip(" \t"))
            if literal_indent is not None:
                if not content.strip() or indentation >= literal_indent:
                    char_start += len(full_line)
                    continue
                literal_indent = None
            if _RST_DIRECTIVE.match(content):
                if _RST_LITERAL.match(content):
                    literal_indent = len(content) - len(content.lstrip(" \t")) + 1
                char_start += len(full_line)
                continue
            if content.rstrip().endswith("::"):
                literal_indent = max(1, indentation + 1)
                content = content.rstrip()[:-1]
            if indentation > 0:
                char_start += len(full_line)
                continue
        else:
            if _ADOC_ATTRIBUTE.match(content) or content.startswith("//"):
                char_start += len(full_line)
                continue
            if content.lstrip().startswith("=") and re.match(r"^\s*={1,6}\s", content):
                prefix = len(content) - len(content.lstrip())
                marker_length = len(content[prefix:]) - len(content[prefix:].lstrip("="))
                content = content[prefix + marker_length :]
                content = content.lstrip(" ")
                old_char_start = char_start
                heading_start = full_line.find(content)
                if content and heading_start >= 0:
                    visible = content.strip()
                    if not _is_machine_literal(visible):
                        protected = _markup_protected(
                            content, old_char_start + heading_start, "adoc"
                        )
                        fragments.append(
                            FragmentDraft(
                                "plain_text",
                                "asciidoc:heading",
                                content,
                                old_char_start + heading_start,
                                old_char_start + heading_start + len(content),
                                protected,
                            )
                        )
                char_start += len(full_line)
                continue
        stripped = content.strip()
        if not stripped or stripped.startswith((".. ", "//", "[#", "[source", "[literal")):
            char_start += len(full_line)
            continue
        if dialect == "rst" and re.fullmatch(r"[=~`:#*+\-^\"']{3,}", stripped):
            char_start += len(full_line)
            continue
        if dialect == "adoc" and stripped.startswith("[") and stripped.endswith("]"):
            char_start += len(full_line)
            continue
        prefix_length = len(content) - len(content.lstrip())
        if dialect == "adoc":
            bullet = re.match(r"\s*(?:\*+|\.+|-|\d+\.)\s+", content)
            if bullet:
                prefix_length = bullet.end()
        text = content[prefix_length:]
        text_start = char_start + prefix_length
        visible = text
        protected = _markup_protected(text, text_start, dialect)
        for span in reversed(protected):
            left, right = span.start_char - text_start, span.end_char - text_start
            visible = visible[:left] + " " * (right - left) + visible[right:]
        if visible.strip() and not _is_machine_literal(visible):
            fragments.append(
                FragmentDraft(
                    "plain_text",
                    "restructured_text:prose" if dialect == "rst" else "asciidoc:prose",
                    text,
                    text_start,
                    text_start + len(text),
                    protected,
                )
            )
        char_start += len(full_line)
    if dialect == "adoc" and in_block:
        diagnostics.append("unclosed_literal_or_code_block")
    fragments.sort(key=lambda item: (item.start_char, item.end_char, item.context))
    return fragments, diagnostics


def extract_plain_text(source: SourceMap) -> tuple[list[FragmentDraft], list[str]]:
    fragments: list[FragmentDraft] = []
    char_start = 0
    for full_line in source.text.splitlines(keepends=True):
        text = full_line.rstrip("\r\n")
        left_trim = len(text) - len(text.lstrip())
        right_trim = len(text.rstrip())
        if left_trim >= right_trim:
            char_start += len(full_line)
            continue
        start, end = char_start + left_trim, char_start + right_trim
        value = source.text[start:end]
        if _is_machine_literal(value):
            char_start += len(full_line)
            continue
        fragments.append(
            FragmentDraft(
                "plain_text",
                "text:line",
                value,
                start,
                end,
                _inline_protection(value, start, "comment"),
            )
        )
        char_start += len(full_line)
    return fragments, []
