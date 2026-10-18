from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from language_review import __version__
from language_review.config import LoadedConfig, load_config
from language_review.doctor import doctor_exit_code, doctor_human, run_doctor
from language_review.errors import ConfigurationError, InputError, OutputError
from language_review.extract_report import extract_document, extract_human
from language_review.extraction import extract_paths
from language_review.report import scan_document, scan_human, write_report
from language_review.scanner import scan_paths


def _add_selection_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("paths", nargs="+", metavar="ПУТЬ")
    command.add_argument("--format", choices=("human", "json"), default="human")
    command.add_argument("--output", type=Path, metavar="ФАЙЛ", help="сохранить отчёт в файл")
    command.add_argument("--profile", metavar="ИМЯ")
    command.add_argument(
        "--extension",
        action="append",
        default=[],
        metavar=".EXT",
        help="добавить расширение; параметр можно повторять",
    )
    command.add_argument(
        "--only-extension",
        action="append",
        default=None,
        metavar=".EXT",
        help="использовать только указанные расширения",
    )
    command.add_argument("--exclude", action="append", default=[], metavar="ШАБЛОН")
    command.add_argument(
        "--respect-gitignore",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="учитывать правила .gitignore (по умолчанию включено)",
    )
    command.add_argument(
        "--follow-symlinks",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="обходить внутренние символические ссылки (по умолчанию выключено)",
    )
    command.add_argument("--max-file-size", type=int, metavar="БАЙТЫ")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="language-review",
        description=(
            "Инвентаризация текстовых файлов перед языковым ревью без изменения исходников."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        metavar="ПУТЬ",
        help="явный TOML-конфиг (объединяется поверх пользовательского)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="инвентаризировать файлы и их базовые характеристики")
    _add_selection_arguments(scan)

    extract = commands.add_parser(
        "extract", help="извлечь человекочитаемые фрагменты с точными диапазонами исходника"
    )
    _add_selection_arguments(extract)

    doctor = commands.add_parser("doctor", help="проверить пакет, конфигурацию и заданные сервисы")
    doctor.add_argument("paths", nargs="*", metavar="ПУТЬ")
    doctor.add_argument("--format", choices=("human", "json"), default="human")

    config = commands.add_parser("config", help="показать эффективную конфигурацию")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    show = config_commands.add_parser("show", help="показать значения и источники")
    show.add_argument("--format", choices=("human", "json"), default="human")
    show.add_argument("--profile", metavar="ИМЯ")
    return parser


def _normalize_extensions(values: list[str] | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    normalized: list[str] = []
    for raw in values:
        value = raw.strip().lower()
        if not value:
            raise InputError("Расширение не может быть пустым.")
        if not value.startswith("."):
            value = f".{value}"
        if any(char in value for char in "/\\*?"):
            raise InputError(f"Некорректное расширение: {raw!r}.")
        normalized.append(value)
    return tuple(dict.fromkeys(normalized))


def _cli_overrides(arguments: argparse.Namespace) -> dict[str, Any]:
    max_size = arguments.max_file_size
    if max_size is not None and max_size < 1:
        raise InputError("--max-file-size должен быть положительным числом.")
    return {
        "extensions_add": _normalize_extensions(arguments.extension),
        "only_extensions": _normalize_extensions(arguments.only_extension),
        "exclude_add": tuple(arguments.exclude),
        "respect_gitignore": arguments.respect_gitignore,
        "follow_symlinks": arguments.follow_symlinks,
        "max_file_size": max_size,
    }


def _config_human(mapping: dict[str, Any]) -> str:
    lines = ["Эффективная конфигурация"]
    files = mapping["config_files"]
    lines.append(f"Пользовательский конфиг: {files['user']}")
    lines.append(f"Загруженные файлы: {', '.join(files['loaded']) or 'нет'}")
    default_profile = mapping["default_profile"]
    lines.append(
        f"Профиль по умолчанию: {default_profile['value']} (источник: {default_profile['source']})"
    )
    if mapping["selected_profile"] is not None:
        lines.append(f"Выбранный профиль: {mapping['selected_profile']}")
    lines.append("Параметры сканирования:")
    for key, item in sorted(mapping["scan"].items()):
        lines.append(f"  {key}: {item['value']} (источник: {item['source']})")
    lines.append("Параметры извлечения:")
    for key, item in sorted(mapping["extract"].items()):
        if key == "selectors":
            if not item:
                lines.append("  selectors: {}")
            for extension, selector_group in sorted(item.items()):
                lines.append(
                    f"  selectors.{extension}: {selector_group['value']} "
                    f"(источник: {selector_group['source']})"
                )
        else:
            lines.append(f"  {key}: {item['value']} (источник: {item['source']})")
    lines.append("Профили:")
    if not mapping["profiles"]:
        lines.append("  не настроены")
    for name, profile in sorted(mapping["profiles"].items()):
        priority = profile["priority"]
        lines.append(f"  {name}: priority={priority['value']} (источник: {priority['source']})")
        for selector in ("project_markers", "match_paths"):
            value = profile[selector]
            lines.append(f"    {selector}: {value['value']} (источник: {value['source']})")
        for key, item in sorted(profile["scan"].items()):
            lines.append(f"    scan.{key}: {item['value']} (источник: {item['source']})")
        for key, item in sorted(profile["extract"].items()):
            if key == "selectors":
                for extension, selector_group in sorted(item.items()):
                    lines.append(
                        f"    extract.selectors.{extension}: {selector_group['value']} "
                        f"(источник: {selector_group['source']})"
                    )
            else:
                lines.append(f"    extract.{key}: {item['value']} (источник: {item['source']})")
    lines.append("Диагностика:")
    lines.append(
        f"  programs: {mapping['doctor']['programs']} "
        f"(источник: {mapping['doctor']['sources']['programs']})"
    )
    lines.append(
        f"  http_services: {mapping['doctor']['http_services']} "
        f"(источник: {mapping['doctor']['sources']['http_services']})"
    )
    return "\n".join(lines)


def _emit(document: Callable[[], Any], human: Callable[[], str], output_format: str) -> str:
    """Построить только запрошенный формат вывода."""
    if output_format == "json":
        return json.dumps(document(), ensure_ascii=False, indent=2, sort_keys=True)
    return human()


def _run(arguments: argparse.Namespace) -> int:
    config: LoadedConfig = load_config(arguments.config)
    if arguments.command == "scan":
        scan_result = scan_paths(
            arguments.paths,
            config,
            requested_profile=arguments.profile,
            cli_overrides=_cli_overrides(arguments),
        )
        content = _emit(
            lambda: scan_document(scan_result), lambda: scan_human(scan_result), arguments.format
        )
        if arguments.output is None:
            print(content)
        else:
            target = write_report(arguments.output, content, scan_result)
            print(f"Отчёт сохранён: {target}", file=sys.stderr)
        if scan_result.input_errors:
            return 2
        return 3 if scan_result.error_count else 0

    if arguments.command == "extract":
        result = extract_paths(
            arguments.paths,
            config,
            requested_profile=arguments.profile,
            cli_overrides=_cli_overrides(arguments),
        )
        content = _emit(
            lambda: extract_document(result), lambda: extract_human(result), arguments.format
        )
        if arguments.output is None:
            print(content)
        else:
            target = write_report(arguments.output, content, result.scan_result)
            print(f"Отчёт сохранён: {target}", file=sys.stderr)
        for document in result.documents:
            for diagnostic in sorted(set(document.diagnostics)):
                print(
                    f"Диагностика {document.root} :: {document.path}: {diagnostic}",
                    file=sys.stderr,
                )
        if result.scan_result.input_errors:
            return 2
        if any(
            document.status in {"error", "partial", "unsupported"} for document in result.documents
        ):
            return 3
        return 0

    if arguments.command == "doctor":
        doctor_result = run_doctor(config, arguments.paths)
        print(_emit(lambda: doctor_result, lambda: doctor_human(doctor_result), arguments.format))
        return doctor_exit_code(doctor_result)

    if arguments.command == "config" and arguments.config_command == "show":
        config_mapping = config.as_mapping(arguments.profile)
        print(
            _emit(lambda: config_mapping, lambda: _config_human(config_mapping), arguments.format)
        )
        return 0

    raise AssertionError(f"Неизвестная команда: {arguments.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        return _run(arguments)
    except ConfigurationError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    except InputError as exc:
        print(f"Ошибка ввода: {exc}", file=sys.stderr)
        return 2
    except OutputError as exc:
        print(f"Ошибка отчёта: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("Операция прервана.", file=sys.stderr)
        return 130
    except OSError as exc:
        print(f"Ошибка ввода-вывода: {exc}", file=sys.stderr)
        return 3
