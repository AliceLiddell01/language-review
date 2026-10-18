from __future__ import annotations

import importlib
import json
import re
import tomllib
from collections.abc import Sequence
from typing import Any

from tree_sitter import Language, Node, Parser

from language_review.config import ExtractSettings
from language_review.coordinates import SourceMap
from language_review.extract_code import (
    _inline_protection,
    _is_machine_literal,
    _merge_spans,
    _walk,
)
from language_review.extract_models import FragmentDraft, ProtectedSpan

STRUCTURED_GRAMMARS: dict[str, tuple[str, str, str]] = {
    ".json": ("json", "tree_sitter_json", "language"),
    ".yaml": ("yaml", "tree_sitter_yaml", "language"),
    ".yml": ("yaml", "tree_sitter_yaml", "language"),
    ".toml": ("toml", "tree_sitter_toml", "language"),
    ".xml": ("xml", "tree_sitter_xml", "language_xml"),
}


def _language(module_name: str, factory_name: str) -> Language:
    module: Any = importlib.import_module(module_name)
    return Language(getattr(module, factory_name)())


def _selector_parts(selector: str) -> tuple[str | int, ...]:
    parts: list[str | int] = []
    index = 1
    while index < len(selector):
        if selector[index] == ".":
            index += 1
            start = index
            while index < len(selector) and selector[index] not in ".[":
                index += 1
            parts.append(selector[start:index])
        elif selector[index] == "[":
            closing = selector.index("]", index + 1)
            item = selector[index + 1 : closing]
            if item.isdecimal():
                parts.append(int(item))
            elif item == "*":
                parts.append("*")
            else:
                parts.append(item[1:-1])
            index = closing + 1
        else:
            raise ValueError(f"Некорректный селектор: {selector}")
    return tuple(parts)


def _selector_matches(path: tuple[str | int, ...], selectors: Sequence[str]) -> bool:
    for selector in selectors:
        parts = _selector_parts(selector)
        if len(parts) == len(path) and all(
            expected == "*" or expected == actual
            for expected, actual in zip(parts, path, strict=True)
        ):
            return True
    return False


def _node_span(source: SourceMap, node: Node) -> tuple[int, int]:
    return source.parser_byte_to_char(node.start_byte), source.parser_byte_to_char(node.end_byte)


def _has_error_ancestor(node: Node) -> bool:
    current: Node | None = node
    while current is not None:
        if current.is_error or current.is_missing:
            return True
        current = current.parent
    return False


def _string_content_span(source: SourceMap, node: Node, language: str) -> tuple[int, int] | None:
    if language == "json":
        start, end = _node_span(source, node)
        if end - start < 2:
            return None
        return start + 1, end - 1
    if language == "yaml":
        if node.type in {"single_quote_scalar", "double_quote_scalar"}:
            start, end = _node_span(source, node)
            if end - start < 2:
                return None
            return start + 1, end - 1
        if node.type == "string_scalar":
            return _node_span(source, node)
        return None
    if language == "toml":
        start, end = _node_span(source, node)
        raw = source.text[start:end]
        for delimiter in ('"""', "'''", '"', "'"):
            if (
                raw.startswith(delimiter)
                and raw.endswith(delimiter)
                and len(raw) >= 2 * len(delimiter)
            ):
                return start + len(delimiter), end - len(delimiter)
        return None
    if language == "xml":
        start, end = _node_span(source, node)
        raw = source.text[start:end]
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            return start + 1, end - 1
        return None
    return None


def _protected(source: SourceMap, start: int, end: int, language: str) -> tuple[ProtectedSpan, ...]:
    text = source.text[start:end]
    spans = list(_inline_protection(text, start, "csharp_log"))
    if language == "xml":
        spans.extend(_inline_protection(text, start, "xml"))
    if language in {"json", "yaml", "toml"}:
        spans.extend(
            ProtectedSpan(start + match.start(), start + match.end(), "escape sequence")
            for match in re.finditer(
                r"\\(?:[\\\"'btnfr/]|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{2})",
                text,
            )
        )
    if language == "yaml":
        spans.extend(
            ProtectedSpan(start + match.start(), start + match.end(), "escape YAML")
            for match in re.finditer(
                r"\\(?:[0abtnvfre \"/\\N_LP]|x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8})|''",
                text,
            )
        )
    if language == "toml":
        spans.extend(
            ProtectedSpan(start + match.start(), start + match.end(), "escape TOML")
            for match in re.finditer(
                r"\\(?:\r?\n[ \t]*|[btnfr\"\\]|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8})", text
            )
        )
    return _merge_spans(spans)


def _draft(
    source: SourceMap,
    language: str,
    path: tuple[str | int, ...],
    span: tuple[int, int] | None,
) -> FragmentDraft | None:
    if span is None:
        return None
    start, end = span
    while start < end and source.text[start].isspace():
        start += 1
    while end > start and source.text[end - 1].isspace():
        end -= 1
    if start >= end:
        return None
    value = source.text[start:end]
    if _is_machine_literal(value):
        return None
    return FragmentDraft(
        "configured_value",
        f"{language}:{'.'.join(str(item) for item in path)}",
        value,
        start,
        end,
        _protected(source, start, end, language),
    )


def _json_key(source: SourceMap, node: Node) -> str | None:
    span = _string_content_span(source, node, "json")
    if span is None:
        return None
    raw = source.text[_node_span(source, node)[0] : _node_span(source, node)[1]]
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def _json_walk(
    source: SourceMap,
    node: Node,
    path: tuple[str | int, ...],
    selectors: Sequence[str],
    output: list[FragmentDraft],
    diagnostics: list[str],
) -> None:
    if node.type == "object":
        entries: list[tuple[str, Node]] = []
        key_counts: dict[str, int] = {}
        for pair in node.named_children:
            if pair.type != "pair" or pair.has_error:
                continue
            key_node = pair.child_by_field_name("key")
            value_node = pair.child_by_field_name("value")
            if key_node is None or value_node is None:
                continue
            key = _json_key(source, key_node)
            if key is None:
                diagnostics.append("unsupported_json_mapping_key")
                continue
            entries.append((key, value_node))
            key_counts[key] = key_counts.get(key, 0) + 1
        if any(count > 1 for count in key_counts.values()):
            diagnostics.append("duplicate_json_mapping_key")
        for key, value_node in entries:
            if key_counts[key] == 1:
                _json_walk(source, value_node, (*path, key), selectors, output, diagnostics)
    elif node.type == "array":
        for index, child in enumerate(node.named_children):
            _json_walk(source, child, (*path, index), selectors, output, diagnostics)
    elif node.type == "string" and _selector_matches(path, selectors):
        draft = _draft(source, "json", path, _string_content_span(source, node, "json"))
        if draft is not None:
            output.append(draft)
    else:
        for child in node.named_children:
            if child.type in {"document", "object", "array", "string"}:
                _json_walk(source, child, path, selectors, output, diagnostics)


def _yaml_key(source: SourceMap, node: Node) -> str | None:
    candidates = [
        item
        for item in _walk(node)
        if item.type in {"string_scalar", "double_quote_scalar", "single_quote_scalar"}
    ]
    if not candidates:
        return None
    start, end = _node_span(source, candidates[-1])
    raw = source.text[start:end]
    if candidates[-1].type == "double_quote_scalar":
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, str) else None
    if candidates[-1].type == "single_quote_scalar" and len(raw) >= 2:
        return raw[1:-1].replace("''", "'")
    return raw if candidates[-1].type == "string_scalar" else None


def _yaml_scalar_node(node: Node) -> Node | None:
    return next(
        (
            item
            for item in _walk(node)
            if item.type
            in {"string_scalar", "double_quote_scalar", "single_quote_scalar", "block_scalar"}
        ),
        None,
    )


def _yaml_block_spans(source: SourceMap, node: Node) -> list[tuple[int, int]]:
    start, end = _node_span(source, node)
    first_line = node.start_point.row
    last_line = node.end_point.row
    spans: list[tuple[int, int]] = []
    base_indent: int | None = None
    for line_number in range(first_line + 1, last_line + 1):
        line_start, line_end, _ = source.line_bounds(line_number)
        raw_line = source.text[line_start:line_end]
        if not raw_line.strip():
            continue
        indentation = len(raw_line) - len(raw_line.lstrip(" \t"))
        if base_indent is None:
            base_indent = indentation
        content_start = line_start + min(base_indent, indentation)
        content_end = line_end
        if content_end > content_start:
            spans.append((content_start, content_end))
    return spans


def _yaml_walk(
    source: SourceMap,
    node: Node,
    path: tuple[str | int, ...],
    selectors: Sequence[str],
    output: list[FragmentDraft],
    diagnostics: list[str],
) -> None:
    if _has_error_ancestor(node):
        diagnostics.append("unsupported_yaml_syntax_branch")
        return
    if node.type in {"flow_node", "block_node"} and any(
        child.type in {"tag", "anchor", "alias"} for child in node.named_children
    ):
        diagnostics.append("unsupported_yaml_tag_or_alias")
        return
    if node.type in {"stream", "document"}:
        for child in node.named_children:
            _yaml_walk(source, child, path, selectors, output, diagnostics)
    elif node.type in {"block_mapping", "flow_mapping"}:
        entries: list[tuple[str, Node]] = []
        key_counts: dict[str, int] = {}
        for pair in node.named_children:
            if pair.type not in {"block_mapping_pair", "flow_pair"} or pair.has_error:
                continue
            key_node = pair.child_by_field_name("key")
            value_node = pair.child_by_field_name("value")
            if key_node is None or value_node is None:
                continue
            key = _yaml_key(source, key_node)
            if key is None:
                diagnostics.append("unsupported_yaml_mapping_key")
                continue
            entries.append((key, value_node))
            key_counts[key] = key_counts.get(key, 0) + 1
        if any(count > 1 for count in key_counts.values()):
            diagnostics.append("duplicate_yaml_mapping_key")
        for key, value_node in entries:
            if key_counts[key] == 1:
                _yaml_walk(source, value_node, (*path, key), selectors, output, diagnostics)
    elif node.type in {"block_sequence", "flow_sequence"}:
        for index, child in enumerate(
            item
            for item in node.named_children
            if item.type in {"block_sequence_item", "flow_node"}
        ):
            _yaml_walk(source, child, (*path, index), selectors, output, diagnostics)
    elif node.type in {
        "block_mapping_pair",
        "block_sequence_item",
        "flow_pair",
        "flow_node",
        "block_node",
    }:
        for child in node.named_children:
            _yaml_walk(source, child, path, selectors, output, diagnostics)
    else:
        scalar = _yaml_scalar_node(node)
        if scalar is None or not _selector_matches(path, selectors):
            return
        if scalar.type == "block_scalar":
            for span in _yaml_block_spans(source, scalar):
                draft = _draft(source, "yaml", path, span)
                if draft is not None:
                    output.append(draft)
        else:
            draft = _draft(source, "yaml", path, _string_content_span(source, scalar, "yaml"))
            if draft is not None:
                output.append(draft)


def _toml_key(node: Node, source: SourceMap) -> tuple[str, ...]:
    if node.type == "dotted_key":
        return tuple(value for child in node.named_children for value in _toml_key(child, source))
    start, end = _node_span(source, node)
    raw = source.text[start:end]
    if node.type == "quoted_key":
        try:
            parsed = tomllib.loads(f"value = {raw}")["value"]
        except tomllib.TOMLDecodeError, KeyError:
            parsed = raw[1:-1]
        return (str(parsed),)
    return (raw,)


def _toml_value(
    source: SourceMap,
    node: Node,
    path: tuple[str | int, ...],
    selectors: Sequence[str],
    output: list[FragmentDraft],
) -> None:
    if _has_error_ancestor(node):
        return
    if node.type == "string":
        if _selector_matches(path, selectors):
            draft = _draft(source, "toml", path, _string_content_span(source, node, "toml"))
            if draft is not None:
                output.append(draft)
    elif node.type == "array":
        for index, child in enumerate(node.named_children):
            _toml_value(source, child, (*path, index), selectors, output)
    elif node.type == "inline_table":
        for pair in node.named_children:
            _toml_pair(source, pair, path, selectors, output)


def _toml_pair(
    source: SourceMap,
    pair: Node,
    parent: tuple[str | int, ...],
    selectors: Sequence[str],
    output: list[FragmentDraft],
) -> None:
    if _has_error_ancestor(pair):
        return
    named = pair.named_children
    if len(named) < 2:
        return
    key_node, value_node = named[0], named[-1]
    path = (*parent, *_toml_key(key_node, source))
    _toml_value(source, value_node, path, selectors, output)


def _toml_walk(
    source: SourceMap,
    root: Node,
    selectors: Sequence[str],
    output: list[FragmentDraft],
    diagnostics: list[str],
) -> None:
    current_table: tuple[str | int, ...] | None = ()
    for node in root.named_children:
        if node.type == "table_array_element":
            diagnostics.append("unsupported_toml_table_array")
            current_table = None
        elif node.type == "table":
            keys: list[str] = []
            for child in node.named_children:
                if child.type in {"bare_key", "quoted_key", "dotted_key"}:
                    keys.extend(_toml_key(child, source))
                elif child.type == "pair":
                    _toml_pair(source, child, tuple(keys), selectors, output)
            current_table = tuple(keys)
        elif node.type == "pair" and current_table is not None:
            _toml_pair(source, node, current_table, selectors, output)


def _xml_name(source: SourceMap, node: Node) -> str | None:
    name_node = next((item for item in _walk(node) if item.type == "Name"), None)
    if name_node is None:
        return None
    start, end = _node_span(source, name_node)
    return source.text[start:end]


def _xml_walk_element(
    source: SourceMap,
    element: Node,
    element_path: tuple[str | int, ...],
    selectors: Sequence[str],
    output: list[FragmentDraft],
    diagnostics: list[str],
) -> None:
    name = _xml_name(
        source, next((item for item in element.named_children if item.type == "STag"), element)
    )
    if name is None or not element_path:
        return
    for child in element.named_children:
        if child.type in {"STag", "EmptyElemTag"}:
            for attribute in child.named_children:
                if attribute.type != "Attribute":
                    continue
                attribute_name = _xml_name(source, attribute)
                value_node = next(
                    (item for item in attribute.named_children if item.type == "AttValue"), None
                )
                if attribute_name is None or value_node is None:
                    continue
                attribute_path = (*element_path, f"@{attribute_name}")
                if _selector_matches(attribute_path, selectors):
                    draft = _draft(
                        source,
                        "xml",
                        attribute_path,
                        _string_content_span(source, value_node, "xml"),
                    )
                    if draft is not None:
                        output.append(draft)
        elif child.type == "content":
            direct_elements = [item for item in child.named_children if item.type == "element"]
            child_names = [
                _xml_name(
                    source,
                    next((item for item in item.named_children if item.type == "STag"), item),
                )
                for item in direct_elements
            ]
            seen_names: dict[str, int] = {}
            for content in child.named_children:
                if content.type == "CharData" and _selector_matches(element_path, selectors):
                    draft = _draft(source, "xml", element_path, _node_span(source, content))
                    if draft is not None:
                        output.append(draft)
                elif content.type in {"CDSect", "CharRef", "EntityRef", "Reference"} and (
                    _selector_matches(element_path, selectors)
                ):
                    diagnostics.append("unsupported_xml_content_node")
                elif content.type == "element":
                    child_name = _xml_name(
                        source,
                        next(
                            (item for item in content.named_children if item.type == "STag"),
                            content,
                        ),
                    )
                    if child_name is None:
                        continue
                    index = seen_names.get(child_name, 0)
                    seen_names[child_name] = index + 1
                    child_path: tuple[str | int, ...] = (*element_path, child_name)
                    if child_names.count(child_name) > 1:
                        child_path = (*child_path, index)
                    _xml_walk_element(source, content, child_path, selectors, output, diagnostics)


def extract_structured(
    source: SourceMap, extension: str, settings: ExtractSettings
) -> tuple[str, list[FragmentDraft], list[str], bool]:
    language, module_name, factory_name = STRUCTURED_GRAMMARS[extension]
    selectors = settings.selector_map().get(extension, ())
    if not selectors:
        return language, [], ["selectors_not_configured"], False
    root = Parser(_language(module_name, factory_name)).parse(source.parser_bytes).root_node
    diagnostics: list[str] = []
    output: list[FragmentDraft] = []
    if language == "json":
        _json_walk(source, root, (), selectors, output, diagnostics)
    elif language == "yaml":
        _yaml_walk(source, root, (), selectors, output, diagnostics)
    elif language == "toml":
        _toml_walk(source, root, selectors, output, diagnostics)
    else:
        for node in root.named_children:
            if node.type == "element":
                name = _xml_name(
                    source,
                    next((item for item in node.named_children if item.type == "STag"), node),
                )
                if name is not None:
                    _xml_walk_element(source, node, (name,), selectors, output, diagnostics)
    has_error = root.has_error
    if has_error:
        diagnostics.append("parser_error_or_missing_node")
    output.sort(key=lambda item: (item.start_char, item.end_char, item.context, item.text))
    return language, output, sorted(set(diagnostics)), has_error or bool(diagnostics)
