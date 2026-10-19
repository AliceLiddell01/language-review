"""Рукотворные данные воспроизводят wire-контракт источников; это не языковой gold."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from support.evaluation_helpers import add_label, make_corpus

from language_review.coordinates import SourceMap
from language_review.errors import InputError
from language_review.evaluation import source_import
from language_review.evaluation.annotation_io import (
    DECISIONS,
    LABELS,
    export_annotations,
    import_annotations,
)
from language_review.evaluation.cli import main
from language_review.evaluation.contracts import jsonl_lines, read_json, save_new
from language_review.evaluation.corpus import (
    annotation_agreement,
    eligible,
    source_map,
    validate_corpus,
)
from language_review.evaluation.corpus import (
    test_hash as frozen_hash,
)
from language_review.evaluation.live import materialize


def wire_source(monkeypatch: pytest.MonkeyPatch, kind: str = "pairs") -> dict[str, bytes]:
    recipe = copy.deepcopy(
        source_import.recipes()["sage-github-ru" if kind == "pairs" else "rust-book-ru"]
    )
    files = {"README.md" if kind == "pairs" else "LICENSE-MIT": b"synthetic license fixture"}
    if kind == "pairs":
        rows = [
            {"source": "Тестт API.", "correction": "Тест API.", "domain": "GitHubTypoCorpusRu"},
            {"source": "Тестт API.", "correction": "Тест API.", "domain": "GitHubTypoCorpusRu"},
            {
                "source": "Пример без исправления.",
                "correction": "Пример без исправления.",
                "domain": "GitHubTypoCorpusRu",
            },
            {
                "source": "Невидимый\u200bсимвол.",
                "correction": "Невидимый символ.",
                "domain": "GitHubTypoCorpusRu",
            },
            {
                "source": "Разделители\u2028\u2029\u0085в строке.",
                "correction": "Разделители в строке.",
                "domain": "GitHubTypoCorpusRu",
            },
        ]
        files["data/GitHubTypoCorpusRu/test.json"] = (
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
        ).encode()
    else:
        files["rustbook-ru/src/demo.md"] = (
            b"\xef\xbb\xbf"
            + (
                "# Раздел\r\n\r\nТекст 😀 с `API` и русскими словами.\r\n\r\nДругой абзац.\r\n"
            ).encode()
        )
    recipe["files"] = [
        {"path": path, "sha256": hashlib.sha256(raw).hexdigest()} for path, raw in files.items()
    ]
    monkeypatch.setattr(source_import, "recipes", lambda: {recipe["id"]: recipe})
    monkeypatch.setattr(
        source_import,
        "_download",
        lambda url: next(raw for path, raw in files.items() if url.endswith("/" + path)),
    )
    return files


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def set_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def decision(packet: Path, verdict: str = "clean", exhaustive: str = "yes") -> None:
    rows = csv_rows(packet / "decisions.csv")
    rows[0].update(
        decision=verdict,
        exhaustive=exhaustive,
        categories="spelling",
        terms="API\nCLI",
        exceptions="Допустимое сокращение",
        note="Synthetic human-input protocol fixture",
    )
    set_csv(packet / "decisions.csv", DECISIONS, rows)


def test_real_pair_wire_import_preserves_test_and_never_promotes_gold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = wire_source(monkeypatch)
    destination = tmp_path / "import"
    assert main(["import-source", "sage-github-ru", "--output", str(destination)]) == 0
    corpus = read_json(destination / "corpus.json")
    manifest = read_json(destination / "provenance.json")
    assert manifest["candidates"] == 3 and manifest["unique_candidates"] == 2
    assert [item["row"] for item in manifest["unsupported_rows"]] == [4, 5]
    assert all(
        case["split"] == "test" and not eligible(case) and not case["labels"]
        for case in corpus["cases"]
    )
    assert all(case["verdict"] == "disputed" for case in corpus["cases"])
    assert (destination / "upstream/data/GitHubTypoCorpusRu/test.json").read_bytes() == files[
        "data/GitHubTypoCorpusRu/test.json"
    ]
    refs = read_json(destination / "references.json")
    assert {
        value["proposal"]["status"] for key, value in refs.items() if key != "unsupported_rows"
    } == {"ambiguous_requires_human", "unchanged_not_attested_clean"}
    corpus["cases"][0]["split"] = "dev"
    with pytest.raises(InputError, match="split"):
        validate_corpus(corpus)


def test_book_bytes_coordinates_protection_and_reproducible_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = wire_source(monkeypatch, "markdown")
    a, b = tmp_path / "a", tmp_path / "b"
    source_import.import_source("rust-book-ru", a)
    corpus = read_json(a / "corpus.json")
    source_import.import_source("rust-book-ru", b, cache=a / "upstream")
    assert (a / "corpus.json").read_bytes() == (b / "corpus.json").read_bytes()
    assert source_map(corpus["documents"][0]).raw == files["rustbook-ru/src/demo.md"]
    assert any(case["protected_ranges"] for case in corpus["cases"])
    materialize(corpus, "dev", tmp_path / "inputs")
    assert (tmp_path / "inputs/rust-book-ru/rustbook-ru/src/demo.md").read_bytes() == files[
        "rustbook-ru/src/demo.md"
    ]
    corpus["split_policy"].update(test_frozen=True, test_sha256=frozen_hash(corpus))
    assert validate_corpus(corpus)


@pytest.mark.parametrize(
    "extension,text",
    [
        (".rs", '// Комментарий о входных данных.\nfn main() { println!("Готово к работе."); }\n'),
        (
            ".cs",
            "// Комментарий о входных данных.\n"
            'class A { void Run() { Console.WriteLine("Готово к работе."); } }\n',
        ),
        (".cpp", '// Комментарий о входных данных.\nvoid run() { printf("Готово к работе."); }\n'),
        (".h", "// Комментарий о входных данных.\nnamespace app { void run(); }\n"),
    ],
)
def test_native_code_import_preserves_original_extension_bytes_and_draft_split(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extension: str,
    text: str,
) -> None:
    recipe = copy.deepcopy(source_import.recipes()["cubic-spline-ru"])
    path = "src/input" + extension
    raw = b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode()
    files = {"LICENSE": b"synthetic permission fixture", path: raw}
    recipe["files"] = [
        {"path": p, "sha256": hashlib.sha256(b).hexdigest()} for p, b in files.items()
    ]
    monkeypatch.setattr(source_import, "recipes", lambda: {recipe["id"]: recipe})
    monkeypatch.setattr(
        source_import,
        "_download",
        lambda url: next(b for p, b in files.items() if url.endswith("/" + p)),
    )
    source_import.import_source(recipe["id"], tmp_path / "import")
    corpus = read_json(tmp_path / "import/corpus.json")
    document = corpus["documents"][0]
    assert document["path"] == path and source_map(document).raw == raw
    assert document["provenance"]["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert all(case["split"] == "validation" and not eligible(case) for case in corpus["cases"])
    assert any(case["context"] == "comment" for case in corpus["cases"])
    if extension != ".h":
        assert any(case["context"] == "message" for case in corpus["cases"])
    materialize(corpus, "validation", tmp_path / "materialized")
    assert (tmp_path / "materialized" / recipe["id"] / path).read_bytes() == raw


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", "main"),
        ("license", "unknown"),
        ("rights_basis", ""),
        ("attribution", ""),
        ("split", "dev"),
        ("base_url", "https://untrusted.invalid"),
    ],
)
def test_source_metadata_rejected_before_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    wire_source(monkeypatch)
    recipe = next(iter(source_import.recipes().values()))
    recipe[field] = value
    monkeypatch.setattr(source_import, "_download", lambda _: pytest.fail("must not fetch"))
    with pytest.raises(InputError):
        source_import.import_source(recipe["id"], tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("target", ["README.md", "data/GitHubTypoCorpusRu/test.json"])
def test_changed_license_or_bytes_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    files = wire_source(monkeypatch)
    files[target] += b"corruption"
    with pytest.raises(InputError, match="изменились"):
        source_import.import_source("sage-github-ru", tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_missing_source_is_sanitized_no_version_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise urllib.error.URLError("private query token")

    monkeypatch.setattr(source_import.urllib.request, "urlopen", unavailable)
    with pytest.raises(InputError, match="недоступен") as error:
        source_import._download("https://example.invalid/pinned")
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "original,corrected,status",
    [
        ("Тестт.", "Тест.", "ambiguous_requires_human"),
        ("Два слова.", "Новые строки!", "ambiguous_requires_human"),
        ("😀\r\nОшабка.", "😀\r\nОшибка.", "draft_unique_splice"),
        ("Без правок.", "Без правок.", "unchanged_not_attested_clean"),
    ],
)
def test_external_pair_ambiguity_is_explicit(original: str, corrected: str, status: str) -> None:
    source = SourceMap.create(b"\xef\xbb\xbf" + original.encode(), "utf-8", "utf-8")
    proposal = source_import.pair_proposal(source, corrected)
    assert proposal["status"] == status
    if proposal["edits"]:
        edit = proposal["edits"][0]
        assert edit["range"]["start"] == source.position(original.index("а")).as_dict()
        assert edit["text"] == "и"


def test_blind_packet_excludes_gold_reference_and_predictions(tmp_path: Path) -> None:
    corpus = make_corpus()
    add_label(corpus, "ошыбка", corrections=("HIDDEN_CORRECTION",))
    corpus["cases"][0]["annotation"]["evidence"] = "HIDDEN_EVIDENCE"
    corpus["cases"][0]["terms"] = ["HIDDEN_TERM"]
    export_annotations(corpus, "dev", tmp_path / "packet")
    content = "".join(path.read_text("utf-8-sig") for path in (tmp_path / "packet").iterdir())
    assert "HIDDEN_" not in content and "independently_verified" not in content
    restored = import_annotations(corpus, tmp_path / "packet", "", "", False)
    assert restored == corpus  # no new confirmation from an untouched packet


def test_jsonl_lines_preserves_unicode_separators_inside_records() -> None:
    value = "До\u2028после\u2029далее\u0085финал"
    contents = json.dumps({"text": value}, ensure_ascii=False) + "\n"
    contents += json.dumps({"text": "следующая запись"}, ensure_ascii=False) + "\n"
    lines = jsonl_lines(contents)
    assert len(lines) == 2
    assert [json.loads(line) for line in lines] == [
        {"text": value},
        {"text": "следующая запись"},
    ]


def test_clean_and_incomplete_require_actual_explicit_attestation(tmp_path: Path) -> None:
    corpus = make_corpus()
    corpus["documents"][0]["data_kind"] = "real"
    corpus["cases"][0].update(
        annotation=source_import.draft_annotation("unreviewed"), exhaustive=False
    )
    packet = tmp_path / "packet"
    export_annotations(corpus, "dev", packet)
    decision(packet)
    with pytest.raises(InputError, match="человека"):
        import_annotations(corpus, packet, "fixture-person", "fixture record", False)
    confirmed = import_annotations(corpus, packet, "fixture-person", "fixture record", True)
    assert eligible(confirmed["cases"][0])
    assert confirmed["cases"][0]["categories"] == ["spelling"]
    assert confirmed["cases"][0]["terms"] == ["API", "CLI"]
    assert confirmed["cases"][0]["annotation"]["method"] == "human"
    assert confirmed["documents"] == corpus["documents"]
    decision(packet, exhaustive="no")
    assert not eligible(
        import_annotations(corpus, packet, "fixture-person", "fixture record", True)["cases"][0]
    )
    decision(packet, verdict="disputed")
    assert not eligible(
        import_annotations(corpus, packet, "fixture-person", "fixture record", True)["cases"][0]
    )


@pytest.mark.parametrize(
    "encoding,bom", [("utf-8", "utf-8"), ("utf-16-le", "utf-16-le"), ("utf-16-be", "utf-16-be")]
)
def test_human_offsets_map_back_to_original_unicode_crlf_bom(
    tmp_path: Path, encoding: str, bom: str
) -> None:
    text = "Заголовок\r\n😀 Ошабка и API."
    corpus = make_corpus(text, encoding=encoding, bom=bom)
    source = source_map(corpus["documents"][0])
    base = text.index("😀")
    corpus["cases"][0]["range"] = source.range_dict(base, len(text))
    packet = tmp_path / "packet"
    export_annotations(corpus, "dev", packet)
    decision(packet, "errors")
    set_csv(
        packet / "labels.csv",
        LABELS,
        [
            {
                "id": "case-1",
                "start": 2,
                "end": 8,
                "quote": "Ошабка",
                "category": "spelling",
                "replace": "yes",
                "replacement": "Ошибка",
                "note": "Protocol fixture",
            }
        ],
    )
    result = import_annotations(corpus, packet, "fixture-person", "fixture record", True)
    label = result["cases"][0]["labels"][0]
    assert label["anchors"] == [source.range_dict(base + 2, base + 8)]
    assert label["anchors"][0]["start"]["utf16_column"] == 4
    assert label["corrections"][0]["text"] == "Ошибка"


@pytest.mark.parametrize(
    "mutation",
    [
        "offset",
        "quote",
        "unknown-id",
        "packet-text",
        "corpus-version",
        "protected",
        "duplicates",
        "answer-column",
        "unreviewed-label",
    ],
)
def test_bad_annotation_roundtrip_rejected(tmp_path: Path, mutation: str) -> None:
    corpus = make_corpus("😀 Ошабка и API.")
    if mutation == "protected":
        corpus["cases"][0]["protected_ranges"] = [
            source_map(corpus["documents"][0]).range_dict(2, 8)
        ]
    packet = tmp_path / "packet"
    export_annotations(corpus, "dev", packet)
    if mutation != "unreviewed-label":
        decision(packet, "errors")
    label = {
        "id": "case-1",
        "start": "2",
        "end": "8",
        "quote": "Ошабка",
        "category": "spelling",
        "replace": "no",
        "replacement": "",
        "note": "test",
    }
    if mutation == "offset":
        label["start"] = "3"  # JS UTF-16 would be wrong here
    if mutation == "quote":
        label["quote"] = "Ошибка"
    if mutation == "unknown-id":
        label["id"] = "missing"
    set_csv(packet / "labels.csv", LABELS, [label])
    if mutation == "packet-text":
        (packet / "fragments.jsonl").write_text("{}\n")
    if mutation == "corpus-version":
        corpus["version"] = "changed"
    if mutation == "duplicates":
        set_csv(packet / "decisions.csv", DECISIONS, csv_rows(packet / "decisions.csv") * 2)
    if mutation == "answer-column":
        (packet / "decisions.csv").write_text(
            (packet / "decisions.csv").read_text("utf-8-sig").replace("note", "predictions", 1),
            encoding="utf-8",
        )
    with pytest.raises(InputError):
        import_annotations(corpus, packet, "fixture-person", "fixture record", True)


def test_cli_roundtrip_agreement_conflict_and_frozen_holdout(tmp_path: Path) -> None:
    corpus = make_corpus()
    original = tmp_path / "corpus.json"
    save_new(original, corpus)
    packet = tmp_path / "packet"
    assert (
        main(["annotation-export", str(original), "--split", "dev", "--output", str(packet)]) == 0
    )
    decision(packet)
    output = tmp_path / "result.json"
    assert (
        main(
            [
                "annotation-import",
                str(original),
                str(packet),
                "--actor",
                "fixture-person",
                "--evidence",
                "fixture record",
                "--attest-human",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    first = read_json(output)
    decision(packet, "disputed")
    second = import_annotations(corpus, packet, "other-fixture-person", "fixture record", True)
    assert annotation_agreement(first, second)["conflicts"] == ["case-1"]
    corpus["cases"][0]["split"] = "test"
    corpus["split_policy"].update(test_frozen=True, test_sha256=frozen_hash(corpus))
    test_packet = tmp_path / "test-packet"
    export_annotations(corpus, "test", test_packet)
    decision(test_packet)
    with pytest.raises(InputError, match="freeze-test"):
        import_annotations(corpus, test_packet, "fixture-person", "fixture record", True)


def test_imported_corpus_cross_split_duplicates_and_hash_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire_source(monkeypatch, "markdown")
    source_import.import_source("rust-book-ru", tmp_path / "import")
    corpus = read_json(tmp_path / "import/corpus.json")
    other = copy.deepcopy(corpus["documents"][0])
    other.update(id="fork-document", repository="translation-fork")
    other["provenance"]["upstream_split"] = "test"
    duplicate = copy.deepcopy(corpus["cases"][0])
    duplicate.update(id="fork-case", document="fork-document", split="test")
    corpus["documents"].append(other)
    corpus["cases"].append(duplicate)
    with pytest.raises(InputError, match="дубликата"):
        validate_corpus(corpus)
    corpus["cases"].pop()
    corpus["documents"].pop()
    corpus["documents"][0]["sha256"] = "0" * 64
    with pytest.raises(InputError, match="SHA-256"):
        validate_corpus(corpus)
