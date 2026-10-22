"""Защищает репозиторий от возврата shell-автоматизации."""

from __future__ import annotations

import ast
import re
import subprocess
from collections import defaultdict
from pathlib import Path, PurePosixPath

from ruamel.yaml import YAML

SHELL_SUFFIXES = {".ps1", ".psm1", ".psd1", ".sh", ".bash", ".zsh", ".bat", ".cmd"}
INPUT_SAMPLE = PurePosixPath("tests/fixtures/extract/sample.ps1")
SHELL_NAMES = {"bash", "sh", "zsh", "pwsh", "powershell", "cmd"}
SHELL_COMMAND = re.compile(
    r"(?i)(?:^|[\s;&|])(?:bash|sh|zsh|pwsh|powershell|cmd)(?:\.exe)?(?:$|[\s;&|])"
)
SHELL_FILE_ARGUMENT = re.compile(
    r"(?i)(?:^|[\s\"'])[^\s\"']+\.(?:ps1|psm1|psd1|sh|bash|zsh|bat|cmd)(?:$|[\s\"'])"
)
SUBPROCESS_NAMES = {
    "run",
    "Popen",
    "call",
    "check_call",
    "check_output",
    "getoutput",
    "getstatusoutput",
}
WRITE_METHODS = {"write_text", "write_bytes"}
COPY_METHODS = {"copy", "copy2", "copyfile", "copyfileobj"}


def _is_shell_path(path: str | PurePosixPath) -> bool:
    return PurePosixPath(path).suffix.lower() in SHELL_SUFFIXES


def _is_documentation_path(path: str | PurePosixPath) -> bool:
    return PurePosixPath(path).suffix.lower() in {".md", ".markdown", ".rst", ".adoc"}


def _looks_like_shell_content(contents: str | bytes) -> bool:
    if isinstance(contents, bytes):
        text = contents[:8192].decode("utf-8", errors="ignore")
    else:
        text = contents[:8192]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    first = lines[0]
    if first.startswith("#!") and re.search(
        r"(?i)(?:^|[/\\\s])(?:bash|sh|zsh|pwsh|powershell|cmd)(?:\.exe)?(?:\s|$)",
        first.removeprefix("#!"),
    ):
        return True
    if first.lower() == "@echo off":
        return True
    lowered = "\n".join(lines).lower()
    powershell_commands = (
        "remove-item",
        "set-content",
        "get-content",
        "new-item",
        "push-location",
        "start-process",
        "write-output",
        "test-path",
        "join-path",
    )
    powershell_markers = sum(command in lowered for command in powershell_commands)
    if "$erroractionpreference" in lowered and powershell_markers:
        return True
    if re.search(r"(?im)^\s*param\s*\(", text) and powershell_markers >= 2:
        return True
    is_shell_prefix = first.lower() == "set -e" or first.lower().startswith("set -euo pipefail")
    return is_shell_prefix and any(
        token in lowered for token in ("\nthen", "\nfi", "\ncase ", "\nesac")
    )


def tracked_shell_files(repository_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repository_root), "ls-files", "--stage", "-z"],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("Не удалось получить список tracked-файлов Git.")
    shell_files: list[str] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            continue
        try:
            mode = metadata.split(maxsplit=1)[0].decode("ascii")
            path = PurePosixPath(raw_path.decode("utf-8"))
        except (IndexError, UnicodeDecodeError) as error:
            raise RuntimeError("Не удалось разобрать список tracked-файлов Git.") from error
        if path == INPUT_SAMPLE:
            continue
        if _is_shell_path(path):
            shell_files.append(f"{mode} {path.as_posix()}")
            continue
        if mode == "100755" and not _is_documentation_path(path):
            tracked_path = repository_root.joinpath(*path.parts)
            if tracked_path.is_file():
                try:
                    contents = tracked_path.read_bytes()
                except OSError as error:
                    raise RuntimeError(
                        "Не удалось прочитать tracked executable-файл Git."
                    ) from error
                if _looks_like_shell_content(contents):
                    shell_files.append(f"{mode} {path.as_posix()}")
    return shell_files


def _constant_string(
    node: ast.AST, assignments: dict[str, list[ast.AST]], seen: set[str] | None = None
) -> str | None:
    seen = set() if seen is None else seen
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in seen:
            return None
        seen.add(node.id)
        values = assignments.get(node.id, [])
        strings = [_constant_string(value, assignments, seen.copy()) for value in values]
        strings = [value for value in strings if value is not None]
        return strings[0] if len(strings) == len(values) and strings else None
    if isinstance(node, ast.JoinedStr):
        tail = ""
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                tail += value.value
            elif isinstance(value, ast.FormattedValue):
                tail = ""
        return tail or None
    return None


def _static_strings(
    node: ast.AST,
    assignments: dict[str, list[ast.AST]],
    seen: set[str] | None = None,
) -> list[str]:
    seen = set() if seen is None else seen
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return [node.value]
        if isinstance(node.value, bytes):
            return [node.value.decode("utf-8", errors="ignore")]
        return []
    if isinstance(node, ast.Name):
        if node.id in seen:
            return []
        seen.add(node.id)
        return [
            text
            for value in assignments.get(node.id, [])
            for text in _static_strings(value, assignments, seen.copy())
        ]
    if isinstance(node, ast.JoinedStr):
        return [
            value.value
            for value in node.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        ]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left, assignments)
        right = _constant_string(node.right, assignments)
        if left is not None and right is not None:
            return [left + right]
        return [
            *_static_strings(node.left, assignments, seen.copy()),
            *_static_strings(node.right, assignments, seen.copy()),
        ]
    return []


def _path_suffix(node: ast.AST, assignments: dict[str, list[ast.AST]]) -> str | None:
    if isinstance(node, ast.Name):
        return next(
            (
                suffix
                for value in assignments.get(node.id, [])
                if (suffix := _path_suffix(value, assignments)) is not None
            ),
            None,
        )
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return PurePosixPath(node.value.replace("\\", "/")).suffix.lower()
    if isinstance(node, ast.JoinedStr):
        tail = ""
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                tail += value.value
            elif isinstance(value, ast.FormattedValue):
                tail = ""
        return PurePosixPath(tail.replace("\\", "/")).suffix.lower() if tail else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
        return _path_suffix(node.right, assignments) or _path_suffix(node.left, assignments)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "with_name" and node.args:
            return _path_suffix(node.args[-1], assignments)
        if node.func.attr == "with_suffix" and node.args:
            suffix = _constant_string(node.args[-1], assignments)
            return PurePosixPath("artifact" + suffix).suffix.lower() if suffix else None
    return None


def _content_is_shell(
    node: ast.AST,
    assignments: dict[str, list[ast.AST]],
    target: ast.AST | None = None,
) -> bool:
    if target is not None and _path_suffix(target, assignments) in {
        ".md",
        ".markdown",
        ".rst",
        ".adoc",
    }:
        return False
    return any(_looks_like_shell_content(value) for value in _static_strings(node, assignments))


def _path_targets_shell_file(
    node: ast.AST,
    assignments: dict[str, list[ast.AST]],
    seen: set[str] | None = None,
) -> bool:
    seen = set() if seen is None else seen
    if isinstance(node, ast.Name):
        if node.id in seen:
            return False
        seen.add(node.id)
        return any(
            _path_targets_shell_file(value, assignments, seen.copy())
            for value in assignments.get(node.id, [])
        )
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _is_shell_path(node.value)
    if isinstance(node, ast.JoinedStr):
        tail = ""
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                tail += value.value
            elif isinstance(value, ast.FormattedValue):
                tail = ""
        return _is_shell_path(tail) if tail else False
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, (ast.Div, ast.Add)):
            return _path_targets_shell_file(node.left, assignments) or _path_targets_shell_file(
                node.right, assignments
            )
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "with_suffix":
            if not node.args:
                return False
            suffix = node.args[-1]
            if isinstance(suffix, ast.Constant) and isinstance(suffix.value, str):
                return _is_shell_path("artifact" + suffix.value)
            return _path_targets_shell_file(node.args[0], assignments)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "with_name":
            if not node.args:
                return False
            name = node.args[-1]
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                return _is_shell_path(name.value)
            return _path_targets_shell_file(node.args[0], assignments)
        if isinstance(node.func, ast.Name) and node.func.id in {
            "Path",
            "PurePath",
            "PurePosixPath",
            "PureWindowsPath",
        }:
            return bool(node.args) and _path_targets_shell_file(node.args[0], assignments)
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "NamedTemporaryFile",
            "TemporaryFile",
        }:
            return any(
                keyword.arg == "suffix"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
                and _is_shell_path("temporary" + keyword.value.value)
                for keyword in node.keywords
            )
    return False


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return ""


def _executable_names(node: ast.AST, assignments: dict[str, list[ast.AST]]) -> set[str]:
    if isinstance(node, ast.Name):
        return (
            set().union(
                *(_executable_names(value, assignments) for value in assignments.get(node.id, []))
            )
            if assignments.get(node.id)
            else set()
        )
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {PurePosixPath(node.value.replace("\\", "/")).name.lower()}
    if isinstance(node, (ast.List, ast.Tuple)):
        values: set[str] = set()
        if node.elts:
            values |= _executable_names(node.elts[0], assignments)
            values |= {
                value.lower()
                for element in node.elts[1:]
                for value in (_constant_string(element, assignments),)
                if value is not None and value.lower() in SHELL_NAMES
            }
        return values
    return set()


def source_automation_errors(source: str, *, scan_writers: bool = True) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return [f"Не удалось разобрать Python-исходник: {error.msg}."]
    assignments: dict[str, list[ast.AST]] = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id].append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            assignments[node.target.id].append(node.value)
    errors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        short_name = name.rsplit(".", maxsplit=1)[-1]
        if short_name in SUBPROCESS_NAMES:
            if any(
                keyword.arg == "shell"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            ):
                errors.append("В Python-автоматизации найден subprocess с shell=True.")
            if node.args and _executable_names(node.args[0], assignments) & SHELL_NAMES:
                errors.append("Python-автоматизация запускает shell-интерпретатор.")
        if name in {"os.system", "os.popen", "os.exec"} or name.startswith("os.exec"):
            errors.append("Python-автоматизация вызывает shell-запуск через os.")
        if scan_writers and short_name in WRITE_METHODS and isinstance(node.func, ast.Attribute):
            if _path_targets_shell_file(node.func.value, assignments):
                errors.append("Python-код генерирует файл shell-сценария.")
            elif node.args and _content_is_shell(node.args[0], assignments, node.func.value):
                errors.append("Python-код генерирует shell-содержимое.")
        if scan_writers and name in {"open", "io.open"} and node.args:
            mode = _constant_string(node.args[1], assignments) if len(node.args) > 1 else "r"
            if (
                mode
                and any(character in mode for character in "wax+")
                and _path_targets_shell_file(node.args[0], assignments)
            ):
                errors.append("Python-код открывает shell-файл для записи.")
        if (
            scan_writers
            and short_name == "write"
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Call)
            and _call_name(node.func.value.func) in {"open", "io.open"}
            and node.func.value.args
            and node.args
        ):
            target = node.func.value.args[0]
            if _path_targets_shell_file(target, assignments):
                errors.append("Python-код записывает shell-файл.")
            elif _content_is_shell(node.args[0], assignments, target):
                errors.append("Python-код записывает shell-содержимое.")
        if scan_writers and short_name in COPY_METHODS and len(node.args) > 1:
            if _path_targets_shell_file(node.args[1], assignments):
                errors.append("Python-код копирует данные в shell-файл.")
        if scan_writers and short_name in {"replace", "rename"} and len(node.args) > 1:
            if _path_targets_shell_file(node.args[1], assignments):
                errors.append("Python-код переименовывает файл в shell-сценарий.")
        if scan_writers and short_name == "writestr":
            target = (
                node.args[0]
                if node.args
                else next(
                    (
                        keyword.value
                        for keyword in node.keywords
                        if keyword.arg in {"zinfo_or_arcname", "arcname"}
                    ),
                    None,
                )
            )
            if target is not None and _path_targets_shell_file(target, assignments):
                errors.append("Python-код добавляет shell-файл в архив.")
            elif (
                target is not None
                and len(node.args) > 1
                and _content_is_shell(node.args[1], assignments, target)
            ):
                errors.append("Python-код добавляет shell-содержимое в архив.")
    if scan_writers:
        generator_words = ("generate", "launcher", "artifact", "package", "bundle", "render")
        for function in (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            if not any(word in function.name.lower() for word in generator_words):
                continue
            local_assignments: dict[str, list[ast.AST]] = defaultdict(list)
            for node in ast.walk(function):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            local_assignments[target.id].append(node.value)
                elif (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.value
                ):
                    local_assignments[node.target.id].append(node.value)
            for node in ast.walk(function):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values, strict=True):
                        if key is not None and _path_targets_shell_file(key, local_assignments):
                            errors.append(
                                f"Python-генератор {function.name} возвращает shell-файл."
                            )
                        elif key is not None and _content_is_shell(value, local_assignments, key):
                            errors.append(
                                f"Python-генератор {function.name} возвращает shell-содержимое."
                            )
    return list(dict.fromkeys(errors))


def python_automation_errors(repository_root: Path) -> list[str]:
    errors: list[str] = []
    roots = ("src/language_review", "scripts", ".github/scripts", "tests")
    for relative_root in roots:
        base = repository_root / relative_root
        if not base.exists():
            continue
        paths = [*base.rglob("*.py"), *base.rglob("*.pyw")]
        for path in sorted(paths):
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                errors.append(f"{path.relative_to(repository_root).as_posix()}: {error}")
                continue
            relative_parts = path.relative_to(repository_root).parts
            is_test = relative_parts[0] == "tests" or path.name.startswith("test_")
            for message in source_automation_errors(source, scan_writers=not is_test):
                errors.append(f"{path.relative_to(repository_root).as_posix()}: {message}")
    return errors


def workflow_errors(repository_root: Path) -> list[str]:
    yaml = YAML(typ="safe")
    errors: list[str] = []
    for path in sorted((repository_root / ".github" / "workflows").glob("*.yml")):
        try:
            document = yaml.load(path.read_text(encoding="utf-8"))
        except Exception as error:
            errors.append(
                f"{path.relative_to(repository_root).as_posix()}: некорректный YAML ({error})."
            )
            continue
        jobs = document.get("jobs", {}) if isinstance(document, dict) else {}
        if not isinstance(jobs, dict):
            errors.append(
                f"{path.relative_to(repository_root).as_posix()}: jobs должен быть объектом."
            )
            continue
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            runs_on = job.get("runs-on")
            windows_job = (
                isinstance(runs_on, str) and runs_on.casefold().startswith("windows")
            ) or (
                isinstance(runs_on, list)
                and any(
                    isinstance(label, str) and label.casefold().startswith("windows")
                    for label in runs_on
                )
            )
            defaults = job.get("defaults", {})
            default_shell = (
                defaults.get("run", {}).get("shell") if isinstance(defaults, dict) else None
            )
            if isinstance(default_shell, str) and default_shell.lower() in SHELL_NAMES:
                errors.append(
                    f"{path.relative_to(repository_root).as_posix()} ({job_name}): "
                    f"задан shell {default_shell}."
                )
            for index, step in enumerate(job.get("steps", [])):
                if not isinstance(step, dict):
                    continue
                where = (
                    f"{path.relative_to(repository_root).as_posix()} ({job_name}, шаг {index + 1})"
                )
                shell = step.get("shell")
                if isinstance(shell, str) and shell.lower() in SHELL_NAMES:
                    errors.append(f"{where}: задан shell {shell}.")
                run = step.get("run")
                if not isinstance(run, str):
                    continue
                lines = run.strip().splitlines()
                effective_shell = shell if isinstance(shell, str) else default_shell
                if windows_job and not (
                    isinstance(effective_shell, str) and effective_shell.lower() == "python"
                ):
                    errors.append(f"{where}: Windows run должен явно использовать shell: python.")
                if isinstance(effective_shell, str) and effective_shell.lower() == "python":
                    errors.extend(
                        f"{where}: {message}" for message in source_automation_errors(run)
                    )
                else:
                    if len(lines) != 1:
                        errors.append(
                            f"{where}: run должен быть одним внешним вызовом, "
                            "а не inline-сценарием."
                        )
                    if SHELL_COMMAND.search(run):
                        errors.append(f"{where}: run запускает shell-интерпретатор.")
                    if SHELL_FILE_ARGUMENT.search(run):
                        errors.append(f"{where}: run ссылается на shell-файл.")
                    if re.search(r"(?i)\bpython(?:3(?:\.\d+)?)?\s+-c\b", run):
                        errors.append(f"{where}: Python-логика не должна оставаться inline в run.")
    return errors


def validate_repository(repository_root: Path) -> list[str]:
    errors: list[str] = []
    try:
        shell_files = tracked_shell_files(repository_root)
    except RuntimeError as error:
        errors.append(str(error))
    else:
        errors.extend(f"Tracked shell-файл: {item}." for item in shell_files)
    errors.extend(python_automation_errors(repository_root))
    errors.extend(workflow_errors(repository_root))
    return errors


def main() -> int:
    repository_root = Path(__file__).resolve().parents[2]
    errors = validate_repository(repository_root)
    if errors:
        print("Найдены источники shell-автоматизации:", flush=True)
        for error in errors:
            print(f"- {error}")
        return 1
    print("Проверка shell-автоматизации пройдена.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
