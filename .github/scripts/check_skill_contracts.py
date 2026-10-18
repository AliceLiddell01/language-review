#!/usr/bin/env python3
"""Проверяет метаданные Agent Skills и локальные ссылки в Markdown."""

import argparse
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
ALLOWED = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
STRING_FIELDS = {"name", "description", "license", "compatibility", "allowed-tools"}
MARKDOWN = MarkdownIt("commonmark")


class RussianArgumentParser(argparse.ArgumentParser):
    """Локализует стандартные заголовки справки argparse."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, add_help=False, **kwargs)
        self.add_argument(
            "-h", "--help", action="help", help="Показать эту справку и завершить работу"
        )
        self._positionals.title = "Позиционные аргументы"
        self._optionals.title = "Параметры"

    def format_help(self) -> str:
        return super().format_help().replace("usage:", "Использование:", 1)


def load_frontmatter(text: str) -> object:
    """Разбирает frontmatter как безопасный YAML 1.2 с запретом повторных ключей."""
    parser = YAML(typ="safe", pure=True)
    parser.max_depth = 50
    return parser.load(text)


def markdown_link_destinations(body: str) -> list[str]:
    """Возвращает назначения настоящих Markdown-ссылок и изображений."""
    destinations: list[str] = []
    for token in MARKDOWN.parse(body):
        if token.type != "inline" or token.children is None:
            continue
        for child in token.children:
            attribute = "src" if child.type == "image" else "href"
            if child.type in {"link_open", "image"} and (target := child.attrGet(attribute)):
                destinations.append(target)
    return destinations


def local_target(destination: str) -> str | None:
    """Возвращает путь локальной Markdown-ссылки или None для внешнего адреса."""
    parsed = urlsplit(destination)
    if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
        return None
    return unquote(parsed.path)


def validate_skill(path: Path) -> list[str]:
    """Проверяет SKILL.md по типам и ограничениям спецификации Agent Skills."""
    errors: list[str] = []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return [f"{path}: не удалось прочитать: {error}"]

    lines = source.splitlines()
    if not lines or lines[0] != "---":
        return [f"{path}: отсутствует начальный YAML frontmatter"]
    try:
        end = lines.index("---", 1)
    except ValueError:
        return [f"{path}: отсутствует закрывающий ---"]

    try:
        data = load_frontmatter("\n".join(lines[1:end]))
    except YAMLError as error:
        mark = getattr(error, "problem_mark", None) or getattr(error, "context_mark", None)
        location = f", строка {mark.line + 2}, столбец {mark.column + 1}" if mark else ""
        return [f"{path}: некорректный YAML frontmatter{location}"]

    if not isinstance(data, Mapping):
        return [f"{path}: YAML frontmatter должен быть отображением"]

    for key, value in data.items():
        if not isinstance(key, str):
            errors.append(f"{path}: ключи YAML frontmatter должны быть строками")
            continue
        if key not in ALLOWED:
            errors.append(
                f"{path}: нестандартное поле {key}; условия активации помещай в description"
            )
        elif key in STRING_FIELDS and not isinstance(value, str):
            errors.append(f"{path}: поле {key} должно быть строкой")

    if "metadata" in data:
        metadata = data["metadata"]
        if not isinstance(metadata, Mapping):
            errors.append(f"{path}: metadata должно быть отображением строковых значений")
        elif any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata.items()
        ):
            errors.append(f"{path}: ключи и значения metadata должны быть строками")

    name = data.get("name")
    if (
        not isinstance(name, str)
        or not NAME.fullmatch(name)
        or len(name) > 64
        or name != path.parent.name
    ):
        errors.append(f"{path}: name должен совпадать с каталогом и отвечать Agent Skills spec")

    description = data.get("description")
    if not isinstance(description, str) or not description or len(description) > 1024:
        errors.append(f"{path}: description должен содержать 1–1024 символа")

    compatibility = data.get("compatibility")
    if isinstance(compatibility, str) and (not compatibility or len(compatibility) > 500):
        errors.append(f"{path}: compatibility должен содержать 1–500 символов")

    skill_root = path.parent.resolve()
    body = "\n".join(lines[end + 1 :])
    for destination in markdown_link_destinations(body):
        target = local_target(destination)
        if target is None:
            continue
        resolved = (skill_root / target).resolve()
        if not resolved.is_relative_to(skill_root) or not resolved.is_file():
            errors.append(f"{path}: несуществующая или выходящая за skill ссылка: {target}")
    return errors


def validate_tree(root: Path) -> list[str]:
    """Проверяет все skills и наличие их обязательных манифестов."""
    base = root / ".agents" / "skills"
    directories = sorted(
        (path for path in base.glob("*") if path.is_dir()), key=lambda path: path.name
    )
    if not directories:
        return ["Нет каталогов skills в .agents/skills/"]
    missing = [
        f"{directory}: отсутствует SKILL.md"
        for directory in directories
        if not (directory / "SKILL.md").is_file()
    ]
    return missing + [
        error for skill in sorted(base.glob("*/SKILL.md")) for error in validate_skill(skill)
    ]


def main() -> int:
    """Проверяет skills в указанном корне репозитория."""
    parser = RussianArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Корень репозитория")
    args = parser.parse_args()
    errors = validate_tree(args.root)
    if errors:
        for error in errors:
            print(f"::error::{error}", file=sys.stderr)
        return 1
    print("Метаданные и ссылки Agent Skills соответствуют контракту")
    return 0


if __name__ == "__main__":
    sys.exit(main())
