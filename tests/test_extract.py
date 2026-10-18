from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from language_review.config import ExtractSettings, load_config
from language_review.coordinates import SourceMap
from language_review.extract_code import extract_code
from language_review.extract_docs import extract_markdown, extract_markup_text, extract_plain_text
from language_review.extract_models import FragmentDraft
from language_review.extract_report import extract_document, extract_json
from language_review.extract_structured import extract_structured
from language_review.extraction import extract_paths
from language_review.scanner import scan_paths

FIXTURES = Path(__file__).parent / "fixtures" / "extract"


def _source(name: str) -> SourceMap:
    return SourceMap.create((FIXTURES / name).read_bytes(), "utf-8", None)


def _protection_texts(source: SourceMap, fragments: list[FragmentDraft]) -> set[str]:
    values = set()
    for fragment in fragments:
        protected_ranges = sorted(
            fragment.protected, key=lambda item: (item.start_char, item.end_char)
        )
        previous_end = fragment.start_char
        for protected in protected_ranges:
            assert fragment.start_char <= protected.start_char
            assert protected.start_char >= previous_end
            assert protected.end_char <= fragment.end_char
            values.add(source.text[protected.start_char : protected.end_char])
            previous_end = protected.end_char
    return values


def test_rust_extraction_keeps_docs_format_fields_and_ignores_machine_values() -> None:
    source = _source("sample.rs")
    result = extract_code(source, ".rs", ExtractSettings())
    values = [fragment.text for fragment in result.fragments]

    assert result.language == "rust"
    assert result.partial is False
    assert "Внутренняя документация: {reader.TokenType}." in values
    assert "Не удалось прочитать {}: {}" in values
    assert "Не удалось открыть {}: {path}" in values
    assert "Повторная попытка чтения для {path}" in values
    assert "Повторите попытку для {reader.TokenType}." in values
    assert "FAILED" not in values
    assert "user.name" not in values
    assert {"{}", "{reader.TokenType}"}.issubset(_protection_texts(source, result.fragments))
    documentation = next(
        fragment
        for fragment in result.fragments
        if fragment.text.startswith("Внутренняя документация")
    )
    assert "{reader.TokenType}" in _protection_texts(source, [documentation])
    for fragment in result.fragments:
        start, end = source.position(fragment.start_char), source.position(fragment.end_char)
        assert source.raw[start.byte_offset : end.byte_offset].decode("utf-8") == fragment.text


def test_rust_documentation_with_generic_types_is_not_parsed_as_xml() -> None:
    source = SourceMap.create(
        b"/// Returns Vec<String> or Option<u8>.\nfn result() {}\n", "utf-8", None
    )

    result = extract_code(source, ".rs", ExtractSettings())

    assert result.partial is False
    assert "documentation_markup_parse_error" not in result.diagnostics
    assert [item.text for item in result.fragments] == ["Returns Vec<String> or Option<u8>."]


def test_csharp_xml_documentation_skips_code_elements() -> None:
    source = SourceMap.create(
        (
            "/// <summary>Возвращает <c>null</c>, если значение не задано.</summary>\n"
            "/// <remarks><code>return null;</code></remarks>\n"
            "class Sample {}\n"
        ).encode(),
        "utf-8",
        None,
    )

    result = extract_code(source, ".cs", ExtractSettings())

    assert result.partial is False
    assert [item.text for item in result.fragments] == ["Возвращает", ", если значение не задано."]


def test_rust_logging_macros_skip_fields_and_select_the_message() -> None:
    source = SourceMap.create(
        (
            'fn run(user: &str) { error!(target: "app", "Не удалось открыть {}", user); '
            'info!(user = %user, "Повторите попытку."); }'
        ).encode(),
        "utf-8",
        None,
    )

    result = extract_code(source, ".rs", ExtractSettings())

    assert result.partial is False
    assert result.diagnostics == []
    assert [item.text for item in result.fragments] == [
        "Не удалось открыть {}",
        "Повторите попытку.",
    ]


def test_csharp_extraction_handles_xml_interpolated_raw_and_verbatim_contexts() -> None:
    source = _source("sample.cs")
    result = extract_code(source, ".cs", ExtractSettings())
    values = [fragment.text for fragment in result.fragments]

    assert result.partial is False
    assert "Не удалось прочитать {Path}." in values
    assert "Не удалось загрузить {Path}" in values
    assert "Готово: {count}" in values
    assert "Повторите попытку: {GetCount(path):N0}" in values
    assert "Не найдено" in values
    assert "Состояние: {GetState(path)}" in values
    assert "Путь C:\\temp следует сохранить" in values
    assert 'Путь ""{Path}"" следует сохранить' in values
    assert "Проверьте состояние {Path}" in values
    assert "Не удалось завершить операцию." in values
    assert "Подробная документация для" in values
    assert "сохраняется." in values
    assert "mumu:7" not in values
    assert "ACK" not in values
    assert {
        "{Path}",
        '""{Path}""',
        "{count}",
        "{GetCount(path):N0}",
        "{GetState(path)}",
        "C:\\temp",
    }.issubset(_protection_texts(source, result.fragments))


def test_csharp_chained_message_call_uses_outer_arguments() -> None:
    source = SourceMap.create(
        "class Example { void Run(Context ctx) { "
        'Log.ForContext(ctx).Error("Не удалось выполнить."); } }'.encode(),
        "utf-8",
        None,
    )

    result = extract_code(source, ".cs", ExtractSettings())

    assert result.partial is False
    assert [fragment.text for fragment in result.fragments] == ["Не удалось выполнить."]


def test_csharp_logger_extracts_message_after_exception_argument() -> None:
    source = SourceMap.create(
        "class Example { void Run(ILogger logger, Exception ex, string path) { "
        'logger.LogError(ex, "Не удалось открыть {Path}", path); } }'.encode(),
        "utf-8",
        None,
    )

    result = extract_code(source, ".cs", ExtractSettings())

    assert result.partial is False
    assert result.diagnostics == []
    assert [item.text for item in result.fragments] == ["Не удалось открыть {Path}"]


def test_cpp_doxygen_printf_raw_and_machine_literals() -> None:
    source = _source("sample.cpp")
    result = extract_code(source, ".cpp", ExtractSettings())
    values = [fragment.text for fragment in result.fragments]

    assert result.language == "cpp"
    assert result.partial is False
    assert "Проверьте настройки." in values
    assert all("@brief" not in value for value in values)
    assert "Не удалось прочитать %s: %d" in values
    assert "Не удалось открыть %s" in values
    assert "Не удалось открыть файл." in values
    assert "ACK" not in values
    protected = _protection_texts(source, result.fragments)
    assert {"%s", "%d"}.issubset(protected)


def test_c_source_comments_messages_and_non_user_literals() -> None:
    source = SourceMap.create(
        (
            "/* C comment. */\nvoid run(const char *path) {\n"
            '  printf("Не удалось открыть %s", path);\n'
            '  const char *protocol = "ACK";\n}\n'
        ).encode(),
        "utf-8",
        None,
    )

    result = extract_code(source, ".c", ExtractSettings())
    values = [fragment.text for fragment in result.fragments]

    assert result.language == "c"
    assert result.partial is False
    assert "C comment." in values
    assert "Не удалось открыть %s" in values
    assert "ACK" not in values
    assert "%s" in _protection_texts(source, result.fragments)


def test_additional_format_string_arguments_do_not_make_c_partial() -> None:
    source = SourceMap.create(b'void run() { printf("%s", "literal"); }', "utf-8", None)

    result = extract_code(source, ".c", ExtractSettings())

    assert result.partial is False
    assert result.diagnostics == []
    assert [item.text for item in result.fragments] == ["%s"]


def test_fprintf_extracts_the_format_argument() -> None:
    source = SourceMap.create(
        'void run(const char *path) { fprintf(stderr, "Не удалось открыть %s", path); }'.encode(),
        "utf-8",
        None,
    )

    result = extract_code(source, ".c", ExtractSettings())

    assert result.partial is False
    assert [fragment.text for fragment in result.fragments] == ["Не удалось открыть %s"]
    assert "%s" in _protection_texts(source, result.fragments)


def test_valid_code_without_message_context_has_no_fragments() -> None:
    source = SourceMap.create(
        b'class Protocol { const string Status = "FAILED"; }\n', "utf-8", None
    )

    result = extract_code(source, ".cs", ExtractSettings())

    assert result.partial is False
    assert result.fragments == []


def test_header_language_auto_and_explicit_selection() -> None:
    raw = b"#pragma once\n// Header prose.\nvoid run();\n"
    source = SourceMap.create(raw, "utf-8", None)
    auto = extract_code(source, ".h", ExtractSettings())
    c_mode = extract_code(source, ".h", ExtractSettings(header_language="c"))
    cpp_mode = extract_code(source, ".h", ExtractSettings(header_language="cpp"))

    assert auto.language == "c_or_cpp_header"
    assert auto.partial is True
    assert "header_language_ambiguous_c_compatible_syntax" in auto.diagnostics
    assert c_mode.language == "c"
    assert cpp_mode.language == "cpp"
    assert cpp_mode.partial is False
    assert [item.text for item in c_mode.fragments] == ["Header prose."]

    namespaced = extract_code(_source("sample.h"), ".h", ExtractSettings())
    assert namespaced.language == "cpp"
    assert namespaced.partial is False
    assert [item.text for item in namespaced.fragments] == ["Внутренний заголовочный комментарий."]


def test_powershell_comment_help_messages_and_here_strings() -> None:
    source = _source("sample.ps1")
    result = extract_code(source, ".ps1", ExtractSettings())
    values = [fragment.text for fragment in result.fragments]

    assert result.language == "powershell"
    assert result.partial is False
    assert "Описание команды для пользователя." in values
    assert "Временное решение" in values
    assert not any(value.endswith("#>") for value in values)
    assert not any(value.startswith(".SYNOPSIS") for value in values)
    assert "Не удалось открыть $Path" in values
    assert "Не удалось повторить чтение для $Path" in values
    assert "Не удалось записать $(Get-Path $Path)" in values
    assert "Готово" in values
    assert "FAILED" not in values
    assert "$Path" in _protection_texts(source, result.fragments)
    assert "$(Get-Path $Path)" in _protection_texts(source, result.fragments)

    module_source = SourceMap.create(
        b"Write-Host @'\nLiteral $Path remains literal\n'@\n", "utf-8", None
    )
    module_result = extract_code(module_source, ".psm1", ExtractSettings())
    assert module_result.partial is False
    assert "Literal $Path remains literal" in [item.text for item in module_result.fragments]


def test_powershell_write_error_extracts_named_message_argument() -> None:
    source = SourceMap.create(
        'Write-Error -Message "Не удалось открыть $Path"'.encode(), "utf-8", None
    )

    result = extract_code(source, ".ps1", ExtractSettings())

    assert result.partial is False
    assert result.diagnostics == []
    assert [item.text for item in result.fragments] == ["Не удалось открыть $Path"]


def test_markdown_maps_duplicate_cells_and_protects_markup() -> None:
    source = _source("sample.md")
    fragments, diagnostics = extract_markdown(source)
    values = [fragment.text for fragment in fragments]

    assert diagnostics == []
    assert "Повторяемая фраза" in values
    assert values.count("Столбец") == 2
    assert values.count("одинаково") == 2
    assert "Цитату не предлагать как прозу." not in " ".join(values)
    assert "Этот кодовый блок не является прозой." not in " ".join(values)
    assert "Metadata that stays out" not in " ".join(values)
    prose = next(value for value in values if "подписью ссылки" in value)
    assert "[подписью ссылки](https://example.test/path)" in prose
    assert "Текст с \\*экранированным знаком\\* и &amp; сущностью." in values
    protected = _protection_texts(source, fragments)
    assert "`внутренней командой`" in protected
    assert any("https://example.test/path" in value for value in protected)
    assert "&amp;" in protected
    assert "\\*" in protected
    assert "{Path}" in protected
    table_cells = [fragment for fragment in fragments if fragment.text == "одинаково"]
    assert table_cells[0].start_char != table_cells[1].start_char
    for fragment in fragments:
        start, end = source.position(fragment.start_char), source.position(fragment.end_char)
        assert source.raw[start.byte_offset : end.byte_offset].decode("utf-8") == fragment.text


def test_markdown_repeated_text_maps_to_distinct_ranges() -> None:
    raw = b"# Same\n\nSame\n"
    source = SourceMap.create(raw, "utf-8", None)
    fragments, diagnostics = extract_markdown(source)

    assert diagnostics == []
    assert [fragment.text for fragment in fragments] == ["Same", "Same"]
    assert fragments[0].start_char != fragments[1].start_char


def test_ambiguous_markdown_mapping_produces_partial_document(
    tmp_path: Path, isolated_appdata: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from language_review import extract_docs

    source_map = SourceMap.create(b"Original source.\n", "utf-8", None)
    token = extract_docs.Token("inline", "", 0)
    token.map = [0, 1]
    token.content = "Text absent from the source"
    assert extract_docs._mapped_inline_lines(source_map, token, {}) is None

    source = tmp_path / "ambiguous.md"
    source.write_text("Проверяем точное сопоставление.\n", encoding="utf-8")
    original_mapping = extract_docs._mapped_inline_lines
    mapping_results = []

    def force_ambiguous_mapping(source_map, inline_token, line_cursors):
        original_content = inline_token.content
        inline_token.content = "Text absent from the source"
        try:
            mapped = original_mapping(source_map, inline_token, line_cursors)
        finally:
            inline_token.content = original_content
        mapping_results.append(mapped)
        return mapped

    monkeypatch.setattr(extract_docs, "_mapped_inline_lines", force_ambiguous_mapping)

    result = extract_paths([str(source)], load_config())

    assert mapping_results == [None]
    assert result.documents[0].status == "partial"
    assert result.documents[0].diagnostics == ["ambiguous_markdown_source_mapping"]


def test_plain_rst_and_asciidoc_keep_daily_prose_and_skip_literal_blocks() -> None:
    txt_source = _source("sample.txt")
    txt, _ = extract_plain_text(txt_source)
    txt_values = [fragment.text for fragment in txt]
    assert "Обычная строка для проверки." in txt_values
    assert "https://example.test/path" not in txt_values
    assert "CONFIG_KEY" not in txt_values
    path_fragment = next(item for item in txt if item.text.startswith("Путь C:"))
    path_text = txt_source.text[
        path_fragment.protected[0].start_char : path_fragment.protected[0].end_char
    ]
    assert path_text == "C:\\Users\\Example Name\\file.txt"

    rst, rst_diagnostics = extract_markup_text(_source("sample.rst"), "rst")
    assert rst_diagnostics == []
    assert "Заголовок документа" in [item.text for item in rst]
    assert "Абзац reStructuredText с ``встроенным кодом`` и обычным текстом." in [
        item.text for item in rst
    ]
    assert not any("Этот блок" in item.text for item in rst)
    assert "встроенный код" not in " ".join(
        _source("sample.rst").text[item.start_char : item.end_char] for item in rst
    )

    adoc, adoc_diagnostics = extract_markup_text(_source("sample.adoc"), "adoc")
    assert adoc_diagnostics == []
    assert "Заголовок AsciiDoc" in [item.text for item in adoc]
    assert "Первый пункт списка." in [item.text for item in adoc]
    assert not any("кодовый блок" in item.text for item in adoc)


def test_plain_text_does_not_protect_slash_words_as_paths() -> None:
    source = SourceMap.create("Используйте чтение/запись и read/write.\n".encode(), "utf-8", None)

    fragments, diagnostics = extract_plain_text(source)

    assert diagnostics == []
    assert [item.text for item in fragments] == ["Используйте чтение/запись и read/write."]
    assert "/запись" not in _protection_texts(source, fragments)
    assert "/write" not in _protection_texts(source, fragments)


def test_structured_extractors_require_selectors_and_keep_exact_source_values() -> None:
    selector_settings = ExtractSettings(
        selectors={
            ".json": ("$.ui.message", "$.ui.protocol"),
            ".yaml": ("$.ui.message", "$.ui.details"),
            ".toml": ("$.ui.message", "$.ui.protocol"),
            ".xml": (
                "$.ui.message",
                "$.ui.button.@title",
                "$.ui.item[1]",
                "$.ui.form.input.@placeholder",
            ),
        }
    )
    expected = {
        "sample.json": ["Не удалось открыть {Path}"],
        "sample.yaml": [
            "Не удалось открыть {Path}",
            "Проверьте параметры.",
            "Повторите попытку.",
        ],
        "sample.toml": ["Не удалось открыть {Path}"],
        "sample.xml": ["Не удалось открыть {Path}", "Продолжить", "Два", "Введите имя"],
    }
    for filename, values in expected.items():
        source = _source(filename)
        language, fragments, diagnostics, partial = extract_structured(
            source, Path(filename).suffix, selector_settings
        )
        assert language in {"json", "yaml", "toml", "xml"}
        assert diagnostics == []
        assert partial is False
        assert [fragment.text for fragment in fragments] == values
        for fragment in fragments:
            start, end = source.position(fragment.start_char), source.position(fragment.end_char)
            assert source.raw[start.byte_offset : end.byte_offset].decode("utf-8") == fragment.text
    language, fragments, diagnostics, partial = extract_structured(
        _source("sample.json"), ".json", ExtractSettings()
    )
    assert language == "json"
    assert fragments == []
    assert diagnostics == ["selectors_not_configured"]
    assert partial is False


def test_xml_selectors_support_namespace_prefixes() -> None:
    source = SourceMap.create(
        "<ui:root><ui:message>Настроенное сообщение.</ui:message></ui:root>".encode(),
        "utf-8",
        None,
    )
    settings = ExtractSettings(selectors={".xml": ("$.ui:root.ui:message",)})

    _language, fragments, diagnostics, partial = extract_structured(source, ".xml", settings)

    assert diagnostics == []
    assert partial is False
    assert [fragment.text for fragment in fragments] == ["Настроенное сообщение."]


@pytest.mark.parametrize(
    "raw",
    [
        "<ui><message>До<![CDATA[между]]>после</message></ui>",
        "<ui><message>До &amp; после</message></ui>",
        "<ui><message>До &#38; после</message></ui>",
    ],
)
def test_xml_selected_unsupported_content_nodes_are_partial(raw: str) -> None:
    source = SourceMap.create(raw.encode(), "utf-8", None)
    settings = ExtractSettings(selectors={".xml": ("$.ui.message",)})

    _language, fragments, diagnostics, partial = extract_structured(source, ".xml", settings)

    assert diagnostics == ["unsupported_xml_content_node"]
    assert partial is True
    assert fragments


def test_structured_escape_sequences_remain_protected() -> None:
    yaml_source = SourceMap.create(
        "ui:\n  message: 'Строка с ''кавычками'' и \\e escape'\n".encode(), "utf-8", None
    )
    toml_source = SourceMap.create(
        '[ui]\nmessage = """Значение \\U0001F600 и \\\n  продолжение"""\n'.encode(),
        "utf-8",
        None,
    )
    yaml_settings = ExtractSettings(selectors={".yaml": ("$.ui.message",)})
    toml_settings = ExtractSettings(selectors={".toml": ("$.ui.message",)})

    _, yaml_fragments, yaml_diagnostics, yaml_partial = extract_structured(
        yaml_source, ".yaml", yaml_settings
    )
    _, toml_fragments, toml_diagnostics, toml_partial = extract_structured(
        toml_source, ".toml", toml_settings
    )

    assert yaml_diagnostics == []
    assert yaml_partial is False
    assert "''" in _protection_texts(yaml_source, yaml_fragments)
    assert "\\e" in _protection_texts(yaml_source, yaml_fragments)
    assert toml_diagnostics == []
    assert toml_partial is False
    assert "\\U0001F600" in _protection_texts(toml_source, toml_fragments)
    assert "\\\n  " in _protection_texts(toml_source, toml_fragments)


def test_json_strings_keep_text_after_escapes_and_allow_empty_keys() -> None:
    raw = r'{"":{"ignored":"Значение"},"ui":{"message":"Первая\nвторая \"часть\""}}'
    source = SourceMap.create(raw.encode("utf-8"), "utf-8", None)
    settings = ExtractSettings(selectors={".json": ("$.ui.message",)})

    _language, fragments, diagnostics, partial = extract_structured(source, ".json", settings)

    assert diagnostics == []
    assert partial is False
    assert len(fragments) == 1
    assert fragments[0].text == r"Первая\nвторая \"часть\""
    assert {"\\n", '\\"'} <= _protection_texts(source, fragments)


def test_yaml_flow_mappings_extract_selected_values() -> None:
    source = SourceMap.create(
        '{ui: {message: "Привет"}, list: ["Первый", "Второй"]}'.encode(),
        "utf-8",
        None,
    )
    settings = ExtractSettings(selectors={".yaml": ("$.ui.message", "$.list[1]")})

    _language, fragments, diagnostics, partial = extract_structured(source, ".yaml", settings)

    assert [fragment.text for fragment in fragments] == ["Привет", "Второй"]
    assert diagnostics == []
    assert partial is False


@pytest.mark.parametrize(
    ("extension", "raw", "selectors", "expected_diagnostic"),
    [
        (
            ".json",
            '{"ui":{"message":"Первый","message":"Второй","safe":"Сохранить"}}',
            ("$.ui.message", "$.ui.safe"),
            "duplicate_json_mapping_key",
        ),
        (
            ".yaml",
            'ui:\n  message: "Первый"\n  message: "Второй"\n  safe: "Сохранить"\n',
            ("$.ui.message", "$.ui.safe"),
            "duplicate_yaml_mapping_key",
        ),
    ],
)
def test_duplicate_structured_mapping_keys_are_skipped(
    extension: str, raw: str, selectors: tuple[str, ...], expected_diagnostic: str
) -> None:
    source = SourceMap.create(raw.encode("utf-8"), "utf-8", None)
    settings = ExtractSettings(selectors={extension: selectors})

    _language, fragments, diagnostics, partial = extract_structured(source, extension, settings)

    assert [fragment.text for fragment in fragments] == ["Сохранить"]
    assert diagnostics == [expected_diagnostic]
    assert partial is True


def test_toml_table_arrays_are_skipped_as_partial() -> None:
    source = SourceMap.create(
        '[[products]]\nmessage = "First product"\n'
        '[[products]]\nmessage = "Second product"\n'
        '[ui]\nmessage = "Обычный раздел"\n'.encode(),
        "utf-8",
        None,
    )
    settings = ExtractSettings(
        selectors={".toml": ("$.products[0].message", "$.products[1].message", "$.ui.message")}
    )

    _language, fragments, diagnostics, partial = extract_structured(source, ".toml", settings)

    assert [fragment.text for fragment in fragments] == ["Обычный раздел"]
    assert diagnostics == ["unsupported_toml_table_array"]
    assert partial is True


def test_yaml_tags_and_aliases_are_skipped_as_partial() -> None:
    source = SourceMap.create(
        (
            'ui:\n  safe: "Безопасное сообщение"\n'
            '  custom: !custom "Не интерпретировать"\n'
            '  base: &shared "Технический шаблон"\n  copy: *shared\n'
        ).encode(),
        "utf-8",
        None,
    )
    settings = ExtractSettings(selectors={".yaml": ("$.ui.safe", "$.ui.custom", "$.ui.copy")})

    language, fragments, diagnostics, partial = extract_structured(source, ".yaml", settings)

    assert language == "yaml"
    assert [fragment.text for fragment in fragments] == ["Безопасное сообщение"]
    assert diagnostics == ["unsupported_yaml_tag_or_alias"]
    assert partial is True


@pytest.mark.parametrize(
    ("extension", "raw"),
    [
        (".json", '{"ui":{"message":"Повреждённая строка"}'),
        (".yaml", 'ui:\n  message: "Незакрытая строка\n'),
        (".toml", '[ui\nmessage = "Повреждённая строка"\n'),
        (".xml", "<ui><message>Незакрытый элемент</ui>"),
    ],
)
def test_malformed_structured_documents_are_partial(extension: str, raw: str) -> None:
    settings = ExtractSettings(selectors={extension: ("$.ui.message",)})
    source = SourceMap.create(raw.encode("utf-8"), "utf-8", None)

    _language, _fragments, diagnostics, partial = extract_structured(source, extension, settings)

    assert partial is True
    assert "parser_error_or_missing_node" in diagnostics


def test_code_parser_errors_skip_strings_from_damaged_calls_but_keep_comments() -> None:
    source = SourceMap.create(
        b'// Safe comment.\nclass Sample { void Run() { LogError("Damaged message"; } }\n',
        "utf-8",
        None,
    )

    result = extract_code(source, ".cs", ExtractSettings())

    assert result.partial is True
    assert "parser_error_or_missing_node" in result.diagnostics
    assert [fragment.text for fragment in result.fragments] == ["Safe comment."]


@pytest.mark.parametrize(
    ("extension", "raw"),
    [
        (".rs", b'fn broken( { println!("Damaged");'),
        (".cs", b'class X { void M() { LogError("Damaged"; } }'),
        (".c", b'int main( { printf("Damaged"); }'),
        (".cpp", b'int main( { std::cerr << "Damaged"; }'),
        (".ps1", b'Write-Error "unterminated'),
    ],
)
def test_code_parser_errors_are_never_reported_as_complete(extension: str, raw: bytes) -> None:
    result = extract_code(SourceMap.create(raw, "utf-8", None), extension, ExtractSettings())

    assert result.partial is True
    assert "parser_error_or_missing_node" in result.diagnostics


def test_source_map_round_trips_utf8_bom_utf16_utf32_and_cp1251() -> None:
    utf16_text = "A😀\r\nБи\u0306\rC\n"
    utf16_raw = b"\xff\xfe" + utf16_text.encode("utf-16-le")
    utf16 = SourceMap.create(utf16_raw, "utf-16-le", "utf-16-le")
    after_emoji = utf16.position(2)
    assert after_emoji.byte_offset == 8
    assert (after_emoji.line, after_emoji.column, after_emoji.utf16_column) == (1, 3, 4)
    start_cyrillic = utf16.position(4)
    assert (start_cyrillic.line, start_cyrillic.column, start_cyrillic.utf16_column) == (2, 1, 1)
    assert start_cyrillic.byte_offset == 12
    assert utf16.position(8).line == 3

    utf32_text = "😀\r"
    utf32 = SourceMap.create(
        b"\x00\x00\xfe\xff" + utf32_text.encode("utf-32-be"),
        "utf-32-be",
        "utf-32-be",
    )
    assert utf32.position(1).byte_offset == 8
    assert utf32.position(2).line == 2

    utf8_text = "Ёж"
    utf8 = SourceMap.create(b"\xef\xbb\xbf" + utf8_text.encode("utf-8"), "utf-8", "utf-8")
    assert utf8.position(0).byte_offset == 3
    assert utf8.position(1).byte_offset == 5

    cp_text = "Привет\rмир"
    cp1251 = SourceMap.create(cp_text.encode("cp1251"), "windows-1251", None)
    assert cp1251.position(7).byte_offset == 7
    assert (cp1251.position(7).line, cp1251.position(7).column) == (2, 1)
    assert cp1251.parser_byte_to_char(len("Привет".encode())) == 6
    with pytest.raises(ValueError):
        cp1251.parser_byte_to_char(1)


def test_extract_uses_scan_selection_profiles_hashes_and_stable_ids(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "config.json").write_text(
        '{"ui":{"message":"Понятное сообщение","protocol":"FAILED"}}', encoding="utf-8"
    )
    (root / "other.yaml").write_text('ui:\n  message: "Читаемый текст"\n', encoding="utf-8")
    (root / "ignored.txt").write_text("Текст вне списка.\n", encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[scan]
extensions = [".json", ".yaml"]
respect_gitignore = false

[extract.selectors]
".json" = ["$.ui.protocol"]
".yaml" = ["$.ui.message"]

[profiles.readable_json]
priority = 5
match_paths = ["*.json"]
[profiles.readable_json.extract.selectors]
".json" = ["$.ui.message"]
""",
        encoding="utf-8",
    )
    config = load_config(config_path)

    first = extract_paths([str(root)], config)
    second = extract_paths([str(root)], config)
    documents = {document.path: document for document in first.documents}
    assert documents["config.json"].profile == "readable_json"
    assert [fragment.text for fragment in documents["config.json"].fragments] == [
        "Понятное сообщение"
    ]
    assert [fragment.text for fragment in documents["other.yaml"].fragments] == ["Читаемый текст"]
    assert documents["ignored.txt"].status == "skipped"
    assert (
        documents["config.json"].source_sha256
        == hashlib.sha256((root / "config.json").read_bytes()).hexdigest()
    )
    assert [
        (item.id, [part.start_char for part in item.fragments]) for item in first.documents
    ] == [(item.id, [part.start_char for part in item.fragments]) for item in second.documents]
    serialized = extract_document(first)
    encoded = extract_json(first)
    assert json.loads(encoded) == serialized
    assert serialized["schema"] == "language-review.extract"
    assert serialized["schema_version"] == "1.0"
    assert str(root) not in encoded
    assert serialized["documents"][0]["source_sha256"]


def test_extract_detects_source_change_after_scan(
    tmp_path: Path, isolated_appdata: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from language_review import extraction

    source = tmp_path / "message.txt"
    source.write_text("Hello.\n", encoding="utf-8")
    config = load_config()
    scanned = scan_paths([str(source)], config)
    source.write_text("World.\n", encoding="utf-8")
    monkeypatch.setattr(extraction, "scan_paths", lambda *args, **kwargs: scanned)

    result = extract_paths([str(source)], config)

    assert result.documents[0].status == "error"
    assert result.documents[0].reason == "source_unavailable_or_changed"
    assert result.documents[0].fragments == []


def test_extract_classifies_adapter_value_error_as_extractor_failure(
    tmp_path: Path, isolated_appdata: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from language_review import extraction

    source = tmp_path / "message.txt"
    source.write_text("Сообщение.\n", encoding="utf-8")
    config = load_config()
    scanned = scan_paths([str(source)], config)
    monkeypatch.setattr(extraction, "scan_paths", lambda *_args, **_kwargs: scanned)

    def fail_adapter(*_args: object, **_kwargs: object) -> None:
        raise ValueError("ошибка адаптера")

    monkeypatch.setattr(extraction, "_adapt", fail_adapter)

    result = extract_paths([str(source)], config)

    assert result.documents[0].status == "error"
    assert result.documents[0].reason == "extractor_failure"
    assert result.documents[0].diagnostics == ["extractor_failure"]


def test_extract_preserves_scanner_error_reason_in_diagnostics(
    tmp_path: Path, isolated_appdata: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from language_review import extraction

    source = tmp_path / "message.txt"
    source.write_text("Сообщение.\n", encoding="utf-8")
    config = load_config()
    scanned = scan_paths([str(source)], config)
    scanned.files[0] = replace(scanned.files[0], status="error", reason="io_error")
    monkeypatch.setattr(extraction, "scan_paths", lambda *_args, **_kwargs: scanned)

    result = extract_paths([str(source)], config)

    assert result.documents[0].status == "error"
    assert result.documents[0].reason == "scan_error"
    assert result.documents[0].diagnostics == ["scan_error", "io_error"]


def test_extract_classifies_fragment_source_mismatch_as_error(
    tmp_path: Path, isolated_appdata: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from language_review import extraction

    source = tmp_path / "message.txt"
    source.write_text("Исходный текст.\n", encoding="utf-8")
    config = load_config()
    scanned = scan_paths([str(source)], config)
    monkeypatch.setattr(extraction, "scan_paths", lambda *_args, **_kwargs: scanned)
    invalid_fragment = FragmentDraft(
        kind="message",
        context="plain_text",
        text="Подменённый текст",
        start_char=0,
        end_char=5,
    )
    monkeypatch.setattr(
        extraction,
        "_adapt",
        lambda *_args, **_kwargs: ("text", "txt", [invalid_fragment], [], False, False),
    )

    result = extract_paths([str(source)], config)

    assert result.documents[0].status == "error"
    assert result.documents[0].reason == "fragment_source_mismatch"
    assert result.documents[0].diagnostics == ["fragment_source_mismatch"]
    assert result.documents[0].fragments == []
    assert json.loads(extract_json(result))["documents"][0]["reason"] == "fragment_source_mismatch"


def test_duplicate_input_has_its_own_root_identity_and_skipped_document(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    source = tmp_path / "message.txt"
    source.write_text("Точное сообщение.\n", encoding="utf-8")

    result = extract_paths([str(source), str(source)], load_config())

    assert [root["id"] for root in result.roots] == ["root-0001", "root-0002"]
    assert len(result.documents) == 2
    selected, duplicate = result.documents
    assert (selected.root, selected.status, selected.path) == (
        "root-0001",
        "extracted",
        "message.txt",
    )
    assert (duplicate.root, duplicate.status, duplicate.reason) == (
        "root-0002",
        "skipped",
        "duplicate_argument",
    )


def test_powershell_data_files_are_explicitly_unsupported(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    source = tmp_path / "settings.psd1"
    source.write_text("@{ Message = 'Не анализировать как код' }", encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[scan]\nextensions = [".psd1"]\nrespect_gitignore = false\n', encoding="utf-8"
    )

    result = extract_paths([str(source)], load_config(config_path))

    document = result.documents[0]
    assert document.status == "unsupported"
    assert document.reason == "powershell_data_file_not_supported"
    assert document.fragments == []


def test_extract_cli_json_output_errors_and_report_protection(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "message.md"
    original = "# Заголовок\n\nТочный текст.\n".encode()
    source.write_bytes(original)
    environment = os.environ.copy()
    environment["APPDATA"] = str(isolated_appdata)
    environment["PYTHONUTF8"] = "1"

    command = [sys.executable, "-m", "language_review", "extract", str(source), "--format", "json"]
    first = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", env=environment
    )
    second = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", env=environment
    )
    assert first.returncode == 0
    assert first.stderr == ""
    first_report = json.loads(first.stdout)
    second_report = json.loads(second.stdout)
    assert first_report["schema"] == "language-review.extract"
    assert [item["id"] for item in first_report["documents"][0]["fragments"]] == [
        item["id"] for item in second_report["documents"][0]["fragments"]
    ]
    assert [item["text"] for item in first_report["documents"][0]["fragments"]] == [
        "Заголовок",
        "Точный текст.",
    ]
    assert source.read_bytes() == original

    report_path = tmp_path / "extract.json"
    saved = subprocess.run(
        [*command, "--output", str(report_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert saved.returncode == 0
    assert saved.stdout == ""
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "ok"

    overwrite = subprocess.run(
        [*command, "--output", str(source)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert overwrite.returncode == 3
    assert "нельзя сохранить поверх" in overwrite.stderr
    assert source.read_bytes() == original

    missing = subprocess.run(
        [
            sys.executable,
            "-m",
            "language_review",
            "extract",
            str(tmp_path / "missing"),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert missing.returncode == 2
    assert json.loads(missing.stdout)["status"] == "input_error"
