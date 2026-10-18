from __future__ import annotations

import importlib
import json
import re
from bisect import bisect_left
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from tree_sitter import Language, Node, Parser

from language_review.config import ExtractSettings
from language_review.coordinates import MappedText, SourceMap
from language_review.extract_models import FragmentDraft, ProtectedSpan

MESSAGE_CALLS = {
    "bail",
    "debug",
    "eprintln",
    "error",
    "fprintf",
    "format",
    "format_args",
    "info",
    "log",
    "panic",
    "perror",
    "printf",
    "println",
    "print",
    "puts",
    "trace",
    "tracing",
    "warn",
    "write",
    "writeln",
    "logcritical",
    "logdebug",
    "logerror",
    "loginformation",
    "logtrace",
    "logwarning",
    "writeline",
    "exception",
    "argumentexception",
    "invalidoperationexception",
    "ioexception",
    "write-error",
    "write-host",
    "write-output",
    "write-warning",
}
CSHARP_LOG_MESSAGE_CALLS = {
    "log",
    "logcritical",
    "logdebug",
    "logerror",
    "loginformation",
    "logtrace",
    "logwarning",
}
CONTAINER_TYPES = {
    "string_literal",
    "raw_string_literal",
    "verbatim_string_literal",
    "interpolated_string_expression",
}
CONTENT_TYPES = {"raw_string_content", "string_content", "string_literal_content"}
COMMENT_TYPES = {"block_comment", "comment", "line_comment"}
GRAMMARS: dict[str, tuple[str, str]] = {
    "rust": ("tree_sitter_rust", "language"),
    "csharp": ("tree_sitter_c_sharp", "language"),
    "c": ("tree_sitter_c", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "powershell": ("tree_sitter_powershell", "language"),
}
CPP_ONLY_NODES = {
    "class_specifier",
    "namespace_definition",
    "namespace_alias_definition",
    "reference_declarator",
    "template_declaration",
    "using_declaration",
}
DIRECTIVE_PREFIXES = (
    "copyright",
    "endregion",
    "license",
    "lgtm",
    "nolint",
    "pragma",
    "region",
    "spdx",
    "todo",
    "fixme",
    "hack",
    "xxx",
)
PATH_OR_URL = re.compile(
    r"(?:https?://\S+|(?<![\w])(?:[A-Za-z]:[\\/]|\\\\|/|\.{1,2}[\\/])\S*)",
    re.IGNORECASE,
)


@dataclass
class CodeExtraction:
    language: str
    fragments: list[FragmentDraft]
    diagnostics: list[str]
    partial: bool


def _language(name: str) -> Language:
    module_name, factory_name = GRAMMARS[name]
    module: Any = importlib.import_module(module_name)
    factory: Callable[[], object] = getattr(module, factory_name)
    return Language(factory())


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _node_has_error_ancestor(node: Node) -> bool:
    current: Node | None = node
    while current is not None:
        if current.is_error or current.is_missing:
            return True
        current = current.parent
    return False


def _source_span(source: SourceMap, node: Node) -> tuple[int, int]:
    return source.parser_byte_to_char(node.start_byte), source.parser_byte_to_char(node.end_byte)


def _merge_spans(spans: Sequence[ProtectedSpan]) -> tuple[ProtectedSpan, ...]:
    ordered = sorted(spans, key=lambda span: (span.start_char, span.end_char, span.reason))
    merged: list[ProtectedSpan] = []
    for span in ordered:
        if span.start_char >= span.end_char:
            continue
        if merged and span.start_char <= merged[-1].end_char:
            previous = merged[-1]
            reasons = sorted(set(previous.reason.split("; ")) | {span.reason})
            merged[-1] = ProtectedSpan(
                previous.start_char, max(previous.end_char, span.end_char), "; ".join(reasons)
            )
        else:
            merged.append(span)
    return tuple(merged)


def _balanced_end(text: str, start: int, opening: str, closing: str) -> int | None:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _brace_protection(text: str, start: int, dialect: str) -> list[ProtectedSpan]:
    spans: list[ProtectedSpan] = []
    index = 0
    while index < len(text):
        if text[index] == "$" and index + 1 < len(text) and text[index + 1] == "{":
            end = _balanced_end(text, index + 1, "{", "}")
            if end is not None:
                spans.append(
                    ProtectedSpan(start + index, start + end, "переменная или placeholder")
                )
                index = end
                continue
        if text[index] == "{" and index + 1 < len(text) and text[index + 1] == "{":
            spans.append(ProtectedSpan(start + index, start + index + 2, "экранированная скобка"))
            index += 2
            continue
        if text[index] == "}" and index + 1 < len(text) and text[index + 1] == "}":
            spans.append(ProtectedSpan(start + index, start + index + 2, "экранированная скобка"))
            index += 2
            continue
        if text[index] == "{" and dialect in {
            "rust_format",
            "rust_raw_format",
            "csharp_log",
            "csharp_verbatim",
            "csharp_raw_format",
            "cpp_format",
            "cpp_raw_format",
            "comment",
            "markdown",
            "xml",
        }:
            end = _balanced_end(text, index, "{", "}")
            if end is not None:
                spans.append(ProtectedSpan(start + index, start + end, "форматное поле"))
                index = end
                continue
        index += 1
    return spans


def _escape_protection(text: str, start: int, dialect: str) -> list[ProtectedSpan]:
    if dialect == "powershell_literal":
        return [
            ProtectedSpan(start + match.start(), start + match.end(), "кавычка PowerShell")
            for match in re.finditer(r"''", text)
        ]
    if dialect == "csharp_verbatim":
        return [
            ProtectedSpan(start + match.start(), start + match.end(), "кавычка C# verbatim")
            for match in re.finditer(r'""', text)
        ]
    # В raw-строках обратная косая черта — обычный символ текста, а не начало escape.
    if dialect in {
        "cpp_raw",
        "cpp_raw_format",
        "rust_raw",
        "rust_raw_format",
        "csharp_raw_format",
    }:
        return []
    spans: list[ProtectedSpan] = []
    index = 0
    if dialect == "powershell":
        while index < len(text):
            if text[index] == chr(96) and index + 1 < len(text):
                spans.append(
                    ProtectedSpan(
                        start + index, start + index + 2, "escape-последовательность PowerShell"
                    )
                )
                index += 2
            elif text[index] == "'" and index + 1 < len(text) and text[index + 1] == "'":
                spans.append(ProtectedSpan(start + index, start + index + 2, "кавычка PowerShell"))
                index += 2
            else:
                index += 1
        return spans
    while index + 1 < len(text):
        if text[index] != "\\":
            index += 1
            continue
        marker = text[index + 1]
        if marker in "abfnrtv\\\"'0":
            end = index + 2
        elif marker == "x":
            end = index + 2
            while end < len(text) and end < index + 4 and text[end] in "0123456789abcdefABCDEF":
                end += 1
        elif marker == "u":
            end = min(len(text), index + 6)
        elif marker == "U":
            end = min(len(text), index + 10)
        else:
            index += 1
            continue
        spans.append(ProtectedSpan(start + index, start + end, "escape-последовательность"))
        index = end
    return spans


def _printf_protection(text: str, start: int) -> list[ProtectedSpan]:
    spans: list[ProtectedSpan] = []
    index = 0
    while index < len(text):
        if text[index] != "%":
            index += 1
            continue
        if index + 1 < len(text) and text[index + 1] == "%":
            spans.append(ProtectedSpan(start + index, start + index + 2, "экранированный %"))
            index += 2
            continue
        cursor = index + 1
        while cursor < len(text) and text[cursor] in "-+ #0'":
            cursor += 1
        position_start = cursor
        while cursor < len(text) and text[cursor].isdigit():
            cursor += 1
        if cursor > position_start and cursor < len(text) and text[cursor] == "$":
            cursor += 1
        if cursor < len(text) and text[cursor] == ".":
            cursor += 1
            while cursor < len(text) and text[cursor].isdigit():
                cursor += 1
        while cursor < len(text) and text[cursor] in "hlLjzt":
            cursor += 1
        if cursor < len(text) and text[cursor] in "diuoxXfFeEgGaAcspn":
            spans.append(ProtectedSpan(start + index, start + cursor + 1, "формат printf"))
            index = cursor + 1
        else:
            index += 1
    return spans


def _inline_protection(text: str, start: int, dialect: str) -> tuple[ProtectedSpan, ...]:
    spans = _brace_protection(text, start, dialect)
    spans.extend(_escape_protection(text, start, dialect))
    if dialect == "powershell":
        spans.extend(
            ProtectedSpan(start + match.start(), start + match.end(), "переменная PowerShell")
            for match in re.finditer(
                r"\$(?:\{[^}]+\}|(?:(?:global|script|local|private|env):)?[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)",
                text,
                re.IGNORECASE,
            )
        )
        index = 0
        while index < len(text) - 1:
            if text.startswith("$(", index):
                end = _balanced_end(text, index + 1, "(", ")")
                if end is not None:
                    spans.append(ProtectedSpan(start + index, start + end, "выражение PowerShell"))
                    index = end
                    continue
            index += 1
    if dialect in {
        "rust_format",
        "rust_raw_format",
        "csharp_log",
        "csharp_raw_format",
        "cpp_format",
        "cpp_raw_format",
        "cpp_raw",
        "rust_raw",
        "csharp_interpolated",
        "csharp_verbatim",
        "printf",
        "powershell",
        "comment",
    }:
        spans.extend(
            ProtectedSpan(start + match.start(), start + match.end(), "URL или путь")
            for match in PATH_OR_URL.finditer(text)
        )
    spans.extend(
        ProtectedSpan(start + match.start(), start + match.end(), "путь с пробелами")
        for match in re.finditer(
            r"(?<![\w])(?:[A-Za-z]:[\\/]|\\\\|/)[^\r\n\"'<>;,]*?\.[A-Za-z0-9]{1,8}(?=$|[\s,;.)!?])",
            text,
        )
    )
    if dialect in {"comment", "markdown", "xml"}:
        index = 0
        while index < len(text):
            if text[index] == "&":
                end = text.find(";", index + 1)
                if 0 <= end - index <= 16:
                    entity = text[index + 1 : end]
                    if entity and all(
                        character.isalnum() or character in "#xX" for character in entity
                    ):
                        spans.append(
                            ProtectedSpan(start + index, start + end + 1, "ссылка на сущность")
                        )
                        index = end + 1
                        continue
            if text[index] == chr(96):
                width = 1
                while index + width < len(text) and text[index + width] == chr(96):
                    width += 1
                close = text.find(chr(96) * width, index + width)
                if close >= 0:
                    spans.append(ProtectedSpan(start + index, start + close + width, "inline-код"))
                    index = close + width
                    continue
            index += 1
    if dialect == "printf":
        spans.extend(_printf_protection(text, start))
    return _merge_spans(spans)


def _is_machine_literal(text: str) -> bool:
    value = text.strip()
    if not value or not any(character.isalpha() for character in value):
        return True
    if PATH_OR_URL.fullmatch(value):
        return True
    if value.startswith(("{", "[")) and value.endswith(("}", "]")):
        try:
            json.loads(value)
        except json.JSONDecodeError, TypeError:
            pass
        else:
            return True
    if re.fullmatch(r"[A-Z][A-Z0-9_:-]{2,}", value):
        return True
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:[.:][A-Za-z0-9_]+)+", value):
        return True
    if not any(character.isspace() for character in value) and re.search(r":\d", value):
        return True
    return value.startswith(("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE"))


def _comment_is_directive(text: str) -> bool:
    value = text.lstrip().lower()
    return any(value.startswith(prefix) for prefix in DIRECTIVE_PREFIXES)


def _comment_views(source: SourceMap, node: Node, language: str) -> tuple[bool, list[MappedText]]:
    start, end = _source_span(source, node)
    raw = source.text[start:end]
    stripped = raw.lstrip()
    is_doc = stripped.startswith(("///", "//!")) or stripped.startswith(("/**", "/*!"))
    powershell_help = language == "powershell" and bool(
        re.search(r"(?im)^\s*\.(?:SYNOPSIS|DESCRIPTION|PARAMETER|EXAMPLE|NOTES|LINK)\b", raw)
    )
    is_doc = is_doc or powershell_help
    block = raw.lstrip().startswith("/*")
    views: list[MappedText] = []
    line_start = start
    first_line = True
    while line_start < end:
        line_end = line_start
        while line_end < end and source.text[line_end] not in "\r\n":
            line_end += 1
        content_start, content_end = line_start, line_end
        while content_start < content_end and source.text[content_start] in " \t":
            content_start += 1
        if first_line:
            if block and source.text.startswith("/*", content_start):
                content_start += 2
                if is_doc and content_start < content_end and source.text[content_start] == "*":
                    content_start += 1
            elif source.text.startswith("//", content_start):
                content_start += 2
                if is_doc and content_start < content_end and source.text[content_start] in "/!":
                    content_start += 1
            elif language == "powershell" and source.text.startswith("<#", content_start):
                content_start += 2
        if language == "powershell":
            if content_start < content_end and source.text.startswith("#>", content_start):
                content_end = content_start
            elif content_start < content_end and source.text[content_start] == "#":
                content_start += 1
            help_match = re.match(
                r"\s*\.(?:SYNOPSIS|DESCRIPTION|PARAMETER|EXAMPLE|NOTES|LINK|INPUTS|OUTPUTS|COMPONENT|ROLE|FUNCTIONALITY)\b[^\r\n]*",
                source.text[content_start:content_end],
                re.IGNORECASE,
            )
            if help_match is not None:
                content_start += help_match.end()
                content_end = content_start
        elif block and content_start < content_end and source.text[content_start] == "*":
            content_start += 1
        if (
            block
            and content_end >= content_start + 2
            and source.text[content_end - 2 : content_end] == "*/"
        ):
            content_end -= 2
        if (
            language == "powershell"
            and content_end >= content_start + 2
            and source.text[content_end - 2 : content_end] == "#>"
        ):
            content_end -= 2
        if is_doc and language in {"rust", "c", "cpp", "csharp"}:
            comment_body = source.text[content_start:content_end]
            directive = re.match(
                r"\s*[@\\](?:brief|details|return|returns|note|warning|deprecated)\b\s*",
                comment_body,
                re.IGNORECASE,
            )
            if directive is not None:
                content_start += directive.end()
            elif re.match(r"\s*[@\\](?:param|tparam|throws|exception)\b", comment_body):
                content_start = content_end
        if content_start < content_end and source.text[content_start] in " \t":
            content_start += 1
        while content_end > content_start and source.text[content_end - 1] in " \t":
            content_end -= 1
        if content_start < content_end:
            views.append(
                MappedText(
                    source.text[content_start:content_end],
                    tuple((index, index + 1) for index in range(content_start, content_end)),
                )
            )
        if line_end >= end:
            break
        line_start = line_end + (2 if source.text.startswith("\r\n", line_end) else 1)
        first_line = False
    return is_doc, views


def _mapped_runs(view: MappedText, start: int, end: int) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    index = start
    while index < end:
        source_start, source_end = view.source_spans[index]
        run_end = source_end
        index += 1
        while index < end and view.source_spans[index][0] == run_end:
            run_end = view.source_spans[index][1]
            index += 1
        runs.append((source_start, run_end))
    return runs


def _join_comment_views(source: SourceMap, views: Sequence[MappedText]) -> MappedText:
    text_parts: list[str] = []
    source_spans: list[tuple[int, int]] = []
    for index, view in enumerate(views):
        if index and views[index - 1].source_spans:
            newline_start = views[index - 1].source_spans[-1][1]
            newline_end = newline_start
            while newline_end < len(source.text) and source.text[newline_end] in "\r\n":
                newline_end += 1
            if newline_end > newline_start:
                text_parts.append("\n")
                source_spans.append((newline_start, newline_end))
        text_parts.append(view.text)
        source_spans.extend(view.source_spans)
    return MappedText("".join(text_parts), tuple(source_spans))


def _xml_doc_fragments(
    source: SourceMap, view: MappedText, context: str
) -> list[FragmentDraft] | None:
    import tree_sitter_xml

    prefix, suffix = "<lr-root>", "</lr-root>"
    parser_source = (prefix + view.text + suffix).encode("utf-8")
    root = Parser(Language(tree_sitter_xml.language_xml())).parse(parser_source).root_node
    if root.has_error:
        return None
    byte_offsets = [0]
    for character in prefix + view.text + suffix:
        byte_offsets.append(byte_offsets[-1] + len(character.encode("utf-8")))
    prefix_bytes = len(prefix.encode("utf-8"))
    content_bytes = len(view.text.encode("utf-8"))
    output: list[FragmentDraft] = []
    for node in _walk(root):
        if node.type != "CharData":
            continue
        first = max(node.start_byte, prefix_bytes)
        last = min(node.end_byte, prefix_bytes + content_bytes)
        if first >= last:
            continue
        view_start = bisect_left(byte_offsets, first) - len(prefix)
        view_end = bisect_left(byte_offsets, last) - len(prefix)
        tag = "xml_text"
        element = node.parent.parent if node.parent is not None else None
        if element is not None:
            for child in element.children:
                if child.type == "STag":
                    name = next((item for item in child.children if item.type == "Name"), None)
                    if name is not None:
                        tag = (name.text or b"").decode("utf-8", errors="replace")
                    break
        if tag in {"c", "code"}:
            continue
        for source_start, source_end in _mapped_runs(view, view_start, view_end):
            raw_text = source.text[source_start:source_end]
            trim_start = len(raw_text) - len(raw_text.lstrip())
            trim_end = len(raw_text.rstrip())
            if trim_start >= trim_end:
                continue
            source_start += trim_start
            source_end = source_start + trim_end - trim_start
            text = source.text[source_start:source_end]
            if _is_machine_literal(text):
                continue
            output.append(
                FragmentDraft(
                    "documentation_comment",
                    f"{context}:{tag}",
                    text,
                    source_start,
                    source_end,
                    _inline_protection(text, source_start, "xml"),
                )
            )
    return output


def _comment_fragments(
    source: SourceMap, root: Node, language: str
) -> tuple[list[FragmentDraft], list[str]]:
    fragments: list[FragmentDraft] = []
    diagnostics: list[str] = []
    nodes = [
        node
        for node in _walk(root)
        if node.type in COMMENT_TYPES and not _node_has_error_ancestor(node)
    ]
    node_index = 0
    while node_index < len(nodes):
        node = nodes[node_index]
        is_doc, views = _comment_views(source, node, language)
        context = f"{language}_{'documentation' if is_doc else 'comment'}"
        if language == "csharp" and is_doc:
            group_views = list(views)
            previous_end = _source_span(source, node)[1]
            next_index = node_index + 1
            while next_index < len(nodes):
                next_node = nodes[next_index]
                next_start, next_end = _source_span(source, next_node)
                gap = source.text[previous_end:next_start]
                separators = re.findall(r"\r\n|\r|\n", gap)
                if (
                    len(separators) != 1
                    or gap.replace(separators[0], "").strip()
                    or not source.text[next_start:next_end].lstrip().startswith("///")
                ):
                    break
                next_is_doc, next_views = _comment_views(source, next_node, language)
                if not next_is_doc:
                    break
                group_views.extend(next_views)
                previous_end = next_end
                next_index += 1
            group_text = "\n".join(view.text for view in group_views)
            if "<" in group_text and ">" in group_text:
                xml_fragments = _xml_doc_fragments(
                    source, _join_comment_views(source, group_views), context
                )
                if xml_fragments is not None:
                    fragments.extend(xml_fragments)
                else:
                    diagnostics.append("documentation_markup_parse_error")
                node_index = next_index
                continue
        for view in views:
            text = view.text
            if _comment_is_directive(text) or _is_machine_literal(text):
                continue
            if any(character in text for character in "();{}=") and not any(
                character.isspace() for character in text
            ):
                continue
            if not view.source_spans:
                continue
            source_start = view.source_spans[0][0]
            source_end = view.source_spans[-1][1]
            fragments.append(
                FragmentDraft(
                    "documentation_comment" if is_doc else "comment",
                    context,
                    source.text[source_start:source_end],
                    source_start,
                    source_end,
                    _inline_protection(text, source_start, "comment"),
                )
            )
        node_index += 1
    return fragments, diagnostics


def _call_node(node: Node, language: str) -> Node | None:
    accepted = {
        "csharp": {"invocation_expression", "object_creation_expression"},
        "cpp": {"call_expression"},
        "c": {"call_expression"},
        "rust": {"macro_invocation"},
        "powershell": {"command"},
    }[language]
    current = node.parent
    while current is not None:
        if current.type in accepted:
            return current
        current = current.parent
    return None


def _call_name(call: Node, language: str) -> str:
    if language in {"csharp", "cpp", "c"}:
        function = call.child_by_field_name("function")
        if function is not None:
            return (function.text or b"").decode("utf-8", errors="replace")
        if language == "csharp" and call.type == "object_creation_expression":
            type_node = call.child_by_field_name("type")
            if type_node is not None:
                return (type_node.text or b"").decode("utf-8", errors="replace")
    elif language == "rust":
        macro = call.child_by_field_name("macro")
        if macro is not None:
            return (macro.text or b"").decode("utf-8", errors="replace")
    elif language == "powershell":
        command_name = call.child_by_field_name("command_name")
        if command_name is not None:
            return (command_name.text or b"").decode("utf-8", errors="replace")
    return ""


def _call_leaf(name: str) -> str:
    return re.split(r"::|->|[.:!]+", re.sub(r"\s+", "", name))[-1].rstrip("!") or "unknown"


def _call_matches(name: str, custom_calls: Sequence[str]) -> bool:
    normalized = re.sub(r"\s+", "", name).lower().rstrip("!")
    if _call_leaf(normalized) in MESSAGE_CALLS:
        return True
    configured = {value.lower().rstrip("!") for value in custom_calls}
    return normalized in configured


def _within(node: Node, container: Node) -> bool:
    return container.start_byte <= node.start_byte and node.end_byte <= container.end_byte


def _is_message_string_argument(container: Node, call: Node, language: str) -> bool:
    if language in {"csharp", "cpp", "c"}:
        argument_list = call.child_by_field_name("arguments")
        if argument_list is not None and argument_list.type == "argument_list":
            arguments = list(argument_list.named_children)
            if language == "csharp":
                arguments = [item for item in arguments if item.type == "argument"]
            if arguments:
                call_leaf = _call_leaf(_call_name(call, language)).lower()
                if language == "csharp" and call_leaf in CSHARP_LOG_MESSAGE_CALLS:
                    for argument in arguments:
                        direct_strings = [
                            item for item in argument.named_children if item.type in CONTAINER_TYPES
                        ]
                        if direct_strings:
                            return any(_within(container, item) for item in direct_strings)
                    return False
                target_index = {"fprintf": 1, "sprintf": 1, "snprintf": 2}.get(call_leaf, 0)
                return target_index < len(arguments) and _within(container, arguments[target_index])
    if language == "rust":
        token_tree = next((item for item in _walk(call) if item.type == "token_tree"), None)
        if token_tree is not None:
            groups: list[list[Node]] = []
            group: list[Node] = []
            for item in token_tree.children:
                if item.type in {"(", ")"}:
                    continue
                if item.type == ",":
                    if group:
                        groups.append(group)
                        group = []
                else:
                    group.append(item)
            if group:
                groups.append(group)
            for arguments in groups:
                if (
                    len(arguments) > 1
                    and arguments[0].type == "identifier"
                    and arguments[1].type in {":", "="}
                ):
                    continue
                strings = [
                    candidate
                    for argument in arguments
                    for candidate in _walk(argument)
                    if candidate.type in CONTAINER_TYPES and not _node_has_error_ancestor(candidate)
                ]
                if strings:
                    return any(_within(container, candidate) for candidate in strings)
            return False
    if language == "powershell":
        elements = next((item for item in _walk(call) if item.type == "command_elements"), None)
        if elements is not None:
            arguments = [
                item for item in elements.named_children if item.type != "command_argument_sep"
            ]
            if arguments:
                for index, item in enumerate(arguments[:-1]):
                    if item.type != "command_parameter":
                        continue
                    parameter = (item.text or b"").decode("utf-8", errors="replace")
                    parameter_name = parameter.lstrip("-").split(":", 1)[0].casefold()
                    if parameter_name in {"message", "object"}:
                        return _within(container, arguments[index + 1])
                return _within(container, arguments[0])
    candidates = [
        item
        for item in _walk(call)
        if item.type in CONTAINER_TYPES and not _node_has_error_ancestor(item)
    ]
    return bool(candidates) and container.start_byte == min(item.start_byte for item in candidates)


def _quoted_content(raw: str, language: str) -> tuple[int, int, str] | None:
    if language == "powershell":
        if raw.startswith(('@"', "@'")):
            newline = raw.find("\n")
            if newline < 0:
                return None
            closing = '"@' if raw.startswith('@"') else "'@"
            terminator = raw.rfind("\n" + closing)
            crlf_terminator = raw.rfind("\r\n" + closing)
            if crlf_terminator >= 0:
                terminator = crlf_terminator
            if terminator < 0:
                return None
            dialect = "powershell" if raw.startswith('@"') else "powershell_literal"
            return newline + 1, terminator, dialect
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            dialect = "powershell" if raw[0] == '"' else "powershell_literal"
            return 1, len(raw) - 1, dialect
        return None
    if language == "rust" and raw.startswith("r"):
        quote = raw.find('"', 1)
        if quote > 0 and set(raw[1:quote]) <= {"#"}:
            hashes = raw[1:quote]
            closing_delimiter = '"' + hashes
            if raw.endswith(closing_delimiter):
                return quote + 1, len(raw) - len(closing_delimiter), "rust_raw"
    if language in {"c", "cpp"} and raw.startswith('R"'):
        opening = raw.find("(")
        closing_index = raw.rfind(")")
        if opening > 1 and closing_index > opening and raw.endswith('"'):
            return opening + 1, closing_index, "cpp_raw"
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        dialect = "rust_format" if language == "rust" else "cpp_format"
        if language == "csharp":
            dialect = "csharp_log"
        return 1, len(raw) - 1, dialect
    return None


def _container_text_fragments(
    source: SourceMap, container: Node, call: Node, language: str
) -> list[FragmentDraft] | None:
    start, end = _source_span(source, container)
    raw = source.text[start:end]
    call_leaf = _call_leaf(_call_name(call, language))
    if container.type == "interpolated_string_expression":
        quote_tokens = [
            item for item in container.children if item.type in {'"', "interpolation_quote"}
        ]
        if len(quote_tokens) < 2:
            return None
        content_start = source.parser_byte_to_char(quote_tokens[0].end_byte)
        content_end = source.parser_byte_to_char(quote_tokens[-1].start_byte)
        text = source.text[content_start:content_end]
        if _is_machine_literal(text):
            return []
        protected = list(_inline_protection(text, content_start, "csharp_interpolated"))
        for interpolation in _walk(container):
            if interpolation.type != "interpolation":
                continue
            interpolation_start, interpolation_end = _source_span(source, interpolation)
            if content_start <= interpolation_start < interpolation_end <= content_end:
                protected.append(
                    ProtectedSpan(
                        interpolation_start,
                        interpolation_end,
                        "выражение интерполяции C#",
                    )
                )
        return [
            FragmentDraft(
                "user_facing_string",
                f"call:{call_leaf}",
                text,
                content_start,
                content_end,
                _merge_spans(protected),
            )
        ]
    if container.type == "raw_string_literal":
        output = []
        for content in _walk(container):
            if content.type not in {"raw_string_content", "string_content"}:
                continue
            content_start, content_end = _source_span(source, content)
            text = source.text[content_start:content_end]
            if not _is_machine_literal(text):
                if language == "rust":
                    dialect = "rust_raw_format"
                elif language == "csharp":
                    dialect = "csharp_raw_format"
                elif call_leaf in {"printf", "fprintf", "perror"}:
                    dialect = "printf"
                else:
                    dialect = "cpp_raw_format"
                output.append(
                    FragmentDraft(
                        "user_facing_string",
                        f"call:{call_leaf}",
                        text,
                        content_start,
                        content_end,
                        _inline_protection(text, content_start, dialect),
                    )
                )
        return output
    if container.type == "verbatim_string_literal":
        if len(raw) < 3 or not raw.startswith('@"') or not raw.endswith('"'):
            return None
        content_start, content_end = start + 2, end - 1
        text = source.text[content_start:content_end]
        if _is_machine_literal(text):
            return []
        return [
            FragmentDraft(
                "user_facing_string",
                f"call:{call_leaf}",
                text,
                content_start,
                content_end,
                _inline_protection(text, content_start, "csharp_verbatim"),
            )
        ]
    parsed = _quoted_content(raw, language)
    if parsed is None:
        content_nodes = [item for item in _walk(container) if item.type in CONTENT_TYPES]
        if not content_nodes:
            return None
        output = []
        for content in content_nodes:
            content_start, content_end = _source_span(source, content)
            text = source.text[content_start:content_end]
            if not _is_machine_literal(text):
                dialect = (
                    "printf"
                    if call_leaf in {"printf", "fprintf", "perror"}
                    else ("rust_format" if language == "rust" else "cpp_format")
                )
                output.append(
                    FragmentDraft(
                        "user_facing_string",
                        f"call:{call_leaf}",
                        text,
                        content_start,
                        content_end,
                        _inline_protection(text, content_start, dialect),
                    )
                )
        return output
    relative_start, relative_end, dialect = parsed
    if call_leaf in {"printf", "fprintf", "perror"}:
        dialect = "printf"
    content_start, content_end = start + relative_start, start + relative_end
    text = source.text[content_start:content_end]
    if _is_machine_literal(text):
        return []
    return [
        FragmentDraft(
            "user_facing_string",
            f"call:{call_leaf}",
            text,
            content_start,
            content_end,
            _inline_protection(text, content_start, dialect),
        )
    ]


def _extract_strings(
    source: SourceMap, root: Node, language: str, custom_calls: Sequence[str]
) -> tuple[list[FragmentDraft], int]:
    output: list[FragmentDraft] = []
    ambiguous = 0
    for container in _walk(root):
        if container.type not in CONTAINER_TYPES or _node_has_error_ancestor(container):
            continue
        call = _call_node(container, language)
        if call is None or not _call_matches(_call_name(call, language), custom_calls):
            continue
        if call.has_error:
            ambiguous += 1
            continue
        if not _is_message_string_argument(container, call, language):
            continue
        fragments = _container_text_fragments(source, container, call, language)
        if fragments is None:
            ambiguous += 1
        else:
            output.extend(fragments)
    return output, ambiguous


def _is_cpp_header(c_tree: Node, cpp_tree: Node) -> tuple[str, Node, bool]:
    if {node.type for node in _walk(cpp_tree)} & CPP_ONLY_NODES:
        return "cpp", cpp_tree, False
    c_errors = sum(node.is_error or node.is_missing for node in _walk(c_tree))
    cpp_errors = sum(node.is_error or node.is_missing for node in _walk(cpp_tree))
    if c_errors != cpp_errors:
        return ("c", c_tree, False) if c_errors < cpp_errors else ("cpp", cpp_tree, False)
    return "c", c_tree, True


def extract_code(source: SourceMap, extension: str, settings: ExtractSettings) -> CodeExtraction:
    diagnostics: list[str] = []
    header_ambiguous = False
    if extension == ".rs":
        language = "rust"
    elif extension == ".cs":
        language = "csharp"
    elif extension == ".c":
        language = "c"
    elif extension in {".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"}:
        language = "cpp"
    elif extension == ".h":
        language = settings.header_language if settings.header_language != "auto" else "c"
    elif extension in {".ps1", ".psm1"}:
        language = "powershell"
    else:
        return CodeExtraction("unknown", [], ["unsupported_source_format"], False)
    if extension == ".h" and settings.header_language == "auto":
        c_tree = Parser(_language("c")).parse(source.parser_bytes).root_node
        cpp_tree = Parser(_language("cpp")).parse(source.parser_bytes).root_node
        language, root, ambiguous_header = _is_cpp_header(c_tree, cpp_tree)
        if ambiguous_header:
            header_ambiguous = True
            language = "c_or_cpp_header"
            diagnostics.append("header_language_ambiguous_c_compatible_syntax")
        parse_language = "c" if language == "c_or_cpp_header" else language
    else:
        parse_language = language
        root = Parser(_language(parse_language)).parse(source.parser_bytes).root_node
    parse_has_error = root.has_error
    if parse_has_error:
        diagnostics.append("parser_error_or_missing_node")
    comments, comment_diagnostics = _comment_fragments(source, root, parse_language)
    diagnostics.extend(comment_diagnostics)
    strings, ambiguous = _extract_strings(source, root, parse_language, settings.message_calls)
    if ambiguous:
        diagnostics.append(f"ambiguous_string_contexts:{ambiguous}")
    fragments = comments + strings
    fragments.sort(key=lambda item: (item.start_char, item.end_char, item.kind, item.context))
    return CodeExtraction(
        language,
        fragments,
        diagnostics,
        parse_has_error
        or header_ambiguous
        or bool(ambiguous)
        or "documentation_markup_parse_error" in diagnostics,
    )
