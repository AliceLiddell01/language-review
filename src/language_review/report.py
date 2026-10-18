from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from language_review import __version__
from language_review.errors import OutputError
from language_review.scanner import FileRecord, ScanResult

REASON_LABELS = {
    "binary_content": "бинарное содержимое",
    "directory_cycle_or_duplicate": "каталог уже обойдён или образует цикл",
    "duplicate_argument": "повторный аргумент",
    "duplicate_path": "файл уже учтён",
    "excluded_directory": "каталог исключён настройками",
    "excluded_pattern": "путь исключён шаблоном",
    "file_too_large": "превышен лимит размера",
    "gitignore": "исключён правилами .gitignore",
    "invalid_encoding": "неподдерживаемая или повреждённая кодировка",
    "io_error": "ошибка ввода-вывода",
    "not_regular_file": "не обычный файл",
    "symbolic_link_not_followed": "символическая ссылка не обходилась",
    "symlink_outside_root": "ссылка выходит за корень сканирования",
    "unsupported_extension": "расширение не включено",
    "unsupported_file_kind": "неподдерживаемый тип пути",
}


def scan_document(result: ScanResult) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "tool": {"name": "language-review", "version": __version__},
        "status": result.status,
        "roots": result.roots,
        "summary": {
            "selected": result.selected_count,
            "skipped": result.skipped_count,
            "errors": result.error_count,
            "roots": len(result.roots),
        },
        "files": [
            {key: value for key, value in asdict(record).items() if key != "_source_sha256"}
            for record in result.files
        ],
    }


def scan_json(result: ScanResult) -> str:
    return json.dumps(
        scan_document(result),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _record_line(record: FileRecord) -> str:
    label = {
        "selected": "ВЫБРАН",
        "skipped": "ПРОПУСК",
        "error": "ОШИБКА",
    }.get(record.status, record.status.upper())
    message = f"  {label:7} {record.root} :: {record.path}"
    if record.file_type:
        message += f" [{record.file_type}]"
    if record.reason:
        message += f" — {REASON_LABELS.get(record.reason, record.reason)}"
    if record.encoding:
        message += f"; {record.encoding}"
    if record.line_endings:
        message += f"; строки: {record.line_endings}"
    if record.size_bytes is not None:
        message += f"; {record.size_bytes} байт"
    if record.error:
        message += f"; {record.error}"
    return message


def scan_human(result: ScanResult) -> str:
    lines = ["Инвентаризация файлов"]
    lines.extend(
        f"Корень: {root['path']} ({root['kind']}, {root['status']})" for root in result.roots
    )
    if result.files:
        lines.extend(_record_line(record) for record in result.files)
    else:
        lines.append("Файлы не обнаружены.")
    lines.append(
        f"Итог: выбрано {result.selected_count}, пропущено {result.skipped_count}, "
        f"ошибок {result.error_count}."
    )
    return "\n".join(lines)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _check_report_target(path: Path, result: ScanResult) -> Path:
    target = path.expanduser().resolve(strict=False)
    resolved = target.resolve(strict=False)
    protected_keys = {
        os.path.normcase(os.path.abspath(target)),
        os.path.normcase(os.path.abspath(resolved)),
    }
    if target.exists() and protected_keys.intersection(result.protected_paths):
        raise OutputError(f"Отчёт нельзя сохранить поверх просканированного файла: {target}")
    for root in result.roots:
        root_path = Path(root["path"])
        try:
            actual_root = root_path.resolve(strict=True)
        except OSError:
            continue
        if root.get("kind") == "directory":
            try:
                target_key = os.path.normcase(os.path.abspath(resolved))
                root_key = os.path.normcase(os.path.abspath(actual_root))
                is_inside = os.path.commonpath((target_key, root_key)) == root_key
            except ValueError:
                is_inside = False
            if is_inside and target.exists():
                raise OutputError(
                    f"Отчёт не будет перезаписывать существующий файл внутри корня: {target}"
                )
        elif _same_path(resolved, actual_root) and target.exists():
            raise OutputError(f"Отчёт нельзя сохранить поверх входного файла: {target}")
    if target.exists() and target.is_dir():
        raise OutputError(f"Путь отчёта является каталогом: {target}")
    if not target.parent.is_dir():
        raise OutputError(f"Каталог для отчёта не существует: {target.parent}")
    return target


def write_report(path: str | Path, content: str, result: ScanResult) -> Path:
    target = _check_report_target(Path(path), result)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = stream.name
            stream.write(content)
            stream.write("\n")
        os.replace(temporary, target)
    except OSError as exc:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise OutputError(f"Не удалось сохранить отчёт {target}: {exc}") from exc
    return target
