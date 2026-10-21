"""Подготовка кандидата и отдельный разрешённый выпуск; обычный CI только проверяет."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

from language_review.release_contract import (
    REPOSITORY,
    RUNTIME_ASSETS,
    SHA,
    ReleaseError,
    digest,
    package_version,
    read_json,
    validate_bundle,
    version_key,
)


def run(*args: str, cwd: Path | None = None) -> str:
    process = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if process.returncode:
        raise ReleaseError("Не выполнена команда: " + args[0] + " " + args[1])
    return process.stdout.strip()


def github(path: str) -> Any:
    return json.loads(run("gh", "api", "repos/" + REPOSITORY + "/" + path))


def release_history() -> list[dict[str, Any]]:
    pages = json.loads(
        run("gh", "api", "--paginate", "--slurp", "repos/" + REPOSITORY + "/releases?per_page=100")
    )
    return [r for page in pages for r in page if not r["draft"]]


def check_target(version: str, commit: str, previous: str, history: list[dict[str, Any]]) -> None:
    key = version_key(version)
    if not SHA.fullmatch(commit):
        raise ReleaseError("Укажите полный неизменный SHA, а не ветку.")
    published = [r for r in history if r.get("published_at")]
    stable = [r for r in published if not r["prerelease"]]
    latest = max(stable, key=lambda r: version_key(r["tag_name"][1:]), default=None)
    if previous != (latest["tag_name"] if latest else "none"):
        raise ReleaseError("Предыдущий стабильный релиз изменился или указан неверно.")
    for release in published:
        other = version_key(release["tag_name"].removeprefix("v"))
        # Prerelease следующей версии не превращает последний stable в prerelease.
        same_channel = bool(release["prerelease"]) == ("-" in version)
        if release["tag_name"] == "v" + version or (
            other >= key and (same_channel or not release["prerelease"])
        ):
            raise ReleaseError("Повторный или более старый выпуск запрещён.")


def notes_valid(text: str, version: str, commit: str, previous: str) -> None:
    required = ("## Изменения", "## Совместимость", "## Миграции и откат", "## Ограничения")
    if (
        not all(
            section in text and text.split(section, 1)[1].split("\n## ", 1)[0].strip()
            for section in required
        )
        or commit not in text
        or version not in text
        or previous not in text
        or re.search(r"\b(TODO|TBD)\b", text)
    ):
        raise ReleaseError(
            "Release notes должны содержать версию/SHA/предыдущий stable и заполненные разделы."
        )


def prepare(args: argparse.Namespace) -> None:
    if os.name != "nt" or sys.version_info[:2] != (3, 15):
        raise ReleaseError("Кандидат Windows требует нативный Windows builder с Python 3.15.")
    history = release_history()
    check_target(args.version, args.commit, args.previous, history)
    if run("git", "rev-parse", args.commit + "^{commit}") != args.commit:
        raise ReleaseError("Commit недоступен локально.")
    if run("git", "ls-remote", "--tags", "origin", "refs/tags/v" + args.version):
        raise ReleaseError("Публичный тег уже существует, его изменение запрещено.")
    notes = args.notes.read_text("utf-8")
    notes_valid(notes, args.version, args.commit, args.previous)
    destination = args.output.absolute()
    if destination.exists():
        raise ReleaseError("Каталог кандидата уже существует.")
    with tempfile.TemporaryDirectory(prefix="language-review-release-") as temp:
        root = Path(temp)
        archive = root / "source.tar"
        run("git", "archive", "--format=tar", "--output=" + str(archive), args.commit)
        tree = root / "source"
        tree.mkdir()
        with tarfile.open(archive) as stream:
            stream.extractall(tree, filter="data")
        locked_inputs = {
            name: (tree / name).read_bytes()
            for name in ("pyproject.toml", "uv.lock", ".python-version")
        }
        run("uv", "sync", "--locked", "--no-install-project", cwd=tree)
        builder_python = tree / ".venv/Scripts/python.exe"
        manifest = tomllib.loads((tree / "pyproject.toml").read_text("utf-8"))
        pep = package_version(args.version)
        identity = {
            "version": args.version,
            "package_version": pep,
            "commit": args.commit,
            "tag": "v" + args.version,
        }
        project = tree / "pyproject.toml"
        project.write_text(
            project.read_text("utf-8").replace(
                'version = "' + manifest["project"]["version"] + '"', 'version = "' + pep + '"', 1
            ),
            encoding="utf-8",
        )
        (tree / "src/language_review/release_identity.json").write_text(
            json.dumps(identity), encoding="utf-8"
        )
        skill = tree / "src/language_review/distribution_assets/SKILL.md"
        skill.write_text(skill.read_text("utf-8").replace("@VERSION@", pep), encoding="utf-8")
        # Экспорт из исходного lock до release-specific замены project version.
        export_tree = root / "export"
        export_tree.mkdir()
        for name in ("pyproject.toml", "uv.lock", ".python-version"):
            (export_tree / name).write_bytes(locked_inputs[name])
        output = root / "candidate"
        output.mkdir()
        for name, extras in {
            "core": [],
            "chatgpt": ["--extra", "chatgpt"],
            "memory": ["--extra", "memory"],
            "all": ["--all-extras"],
        }.items():
            run(
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--no-header",
                *extras,
                "--output-file",
                str(output / f"requirements-{name}.txt"),
                cwd=export_tree,
            )
        # У Python 3.15 часть закреплённых native зависимостей имеет только sdist.
        # Сборка выполняется у издателя, инструменты взяты из uv.lock; на клиенте
        # разрешены только поставленные wheels без компилятора и обращения к index.
        wheelhouse = root / "wheelhouse"
        wheelhouse.mkdir()
        run(
            str(builder_python),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--require-hashes",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheelhouse),
            "-r",
            str(output / "requirements-all.txt"),
        )
        built_hashes = {
            p.name.split("-", 1)[0].lower().replace("_", "-"): digest(p)
            for p in wheelhouse.glob("*.whl")
        }
        for requirement in output.glob("requirements-*.txt"):
            lines = []
            for line in requirement.read_text("utf-8").splitlines():
                lines.append(line)
                match = re.match(r"([A-Za-z0-9_-]+)(?:\[[^]]+\])?==", line)
                if match and line.rstrip().endswith("\\"):
                    checksum = built_hashes.get(match[1].lower().replace("_", "-"))
                    if checksum:
                        lines.append("    --hash=sha256:" + checksum + " \\")
            requirement.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with zipfile.ZipFile(
            output / "wheelhouse.zip", "w", compression=zipfile.ZIP_DEFLATED
        ) as wheels:
            for wheel in sorted(wheelhouse.glob("*.whl")):
                wheels.write(wheel, wheel.name)
        # Build backend закреплён pyproject; разрешение runtime не изменяется.
        run("uv", "build", "--no-sources", "--out-dir", str(output), cwd=tree)
        # uv создаёт служебный .gitignore в out-dir; это не release asset.
        (output / ".gitignore").unlink(missing_ok=True)
        shutil.copyfile(tree / "scripts/install.ps1", output / "install.ps1")
        (output / "INSTALL.md").write_text(
            "# Language Review " + args.version + "\n\n"
            "Windows x64, PowerShell 7. Не требуется checkout или системный Python.\n"
            "Запустите проверенный install.ps1 -Tag " + identity["tag"] + ".\n"
            "После установки: language-review setup; language-review doctor.\n"
            "Проект настраивается отдельно явным language-review init.\n"
            "Доверие: официальный GitHub API/TLS; SHA-256 — integrity, не независимая подпись.\n"
            "Подробности: https://github.com/"
            + REPOSITORY
            + "/blob/"
            + args.commit
            + "/docs/installation.md\n",
            encoding="utf-8",
        )
        for name in RUNTIME_ASSETS:
            shutil.copyfile(
                tree / (name if name == "compose.memory.yml" else "scripts/" + name), output / name
            )
        (output / "release-notes.md").write_text(notes, encoding="utf-8")
        data = {
            "schema": "language-review.release/1",
            "repository": REPOSITORY,
            **identity,
            "channel": args.channel,
            "previous_stable": args.previous,
            "python": "3.15",
            "skill_contract": 1,
            "lock_sha256": digest(export_tree / "uv.lock"),
            "assets": {
                p.name: {"size": p.stat().st_size, "sha256": digest(p)}
                for p in sorted(output.iterdir())
            },
        }
        (output / "release.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        validate_bundle(output)
        (output / "SHA256SUMS").write_text(
            "".join(f"{digest(p)}  {p.name}\n" for p in sorted(output.iterdir())), encoding="utf-8"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(output, destination)
    print("Кандидат подготовлен; публичный тег и GitHub Release не созданы.")


def publish(args: argparse.Namespace) -> None:
    data = validate_bundle(args.candidate)
    # Это внешняя предпосылка CI, а не параметр самого candidate manifest.
    expected = "publish " + data["tag"] + " " + data["commit"]
    if (
        os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("LR_RELEASE_AUTHORIZATION") != expected
        or os.environ.get("LR_NOTES_SHA256") != digest(args.candidate / "release-notes.md")
    ):
        raise ReleaseError(
            "Нет отдельного ручного разрешения выпуска и подтверждения release notes."
        )
    check_target(data["version"], data["commit"], data["previous_stable"], release_history())
    if run("git", "ls-remote", "--tags", "origin", "refs/tags/" + data["tag"]):
        raise ReleaseError("Тег уже существует; повторная публикация запрещена.")
    # Последние обязательные workflow именно на target SHA; старый успех не принимается.
    workflows = github("actions/runs?head_sha=" + data["commit"] + "&per_page=100")["workflow_runs"]
    for filename in ("ci.yml", "codeql.yml"):
        matching = [
            r
            for r in workflows
            if r["path"] == ".github/workflows/" + filename and r["head_sha"] == data["commit"]
        ]
        if not matching or max(matching, key=lambda r: r["id"])["conclusion"] != "success":
            raise ReleaseError("Обязательный CI не завершён успешно для target SHA: " + filename)
    evidence = read_json(args.evidence)
    if (
        evidence.get("commit") != data["commit"]
        or evidence.get("manifest_sha256") != digest(args.candidate / "release.json")
        or evidence.get("status") != "PASS"
    ):
        raise ReleaseError("Нет приёмки Windows именно этого комплекта.")
    notes_valid(
        (args.candidate / "release-notes.md").read_text("utf-8"),
        data["version"],
        data["commit"],
        data["previous_stable"],
    )
    # Создание ref атомарно отказывает при гонке с уже существующим тегом.
    tag_ref = json.loads(
        run(
            "gh",
            "api",
            "repos/" + REPOSITORY + "/git/refs",
            "--method",
            "POST",
            "-f",
            "ref=refs/tags/" + data["tag"],
            "-f",
            "sha=" + data["commit"],
        )
    )
    if (
        tag_ref.get("ref") != "refs/tags/" + data["tag"]
        or tag_ref.get("object", {}).get("sha") != data["commit"]
    ):
        raise ReleaseError("Созданный ref не соответствует разрешённому target.")
    # Сначала draft с полным комплектом; immutable publication происходит последней.
    run(
        "gh",
        "release",
        "create",
        data["tag"],
        "--verify-tag",
        "--repo",
        REPOSITORY,
        "--target",
        data["commit"],
        "--draft",
        "--title",
        "Language Review " + data["tag"],
        "--notes-file",
        str(args.candidate / "release-notes.md"),
        *(["--prerelease"] if data["channel"] == "prerelease" else []),
        *[str(p) for p in sorted(args.candidate.iterdir()) if p.is_file()],
    )
    with tempfile.TemporaryDirectory(prefix="language-review-readback-") as temp:
        run("gh", "release", "download", data["tag"], "--repo", REPOSITORY, "--dir", temp)
        validate_bundle(Path(temp))
        if digest(Path(temp) / "release.json") != digest(args.candidate / "release.json"):
            raise ReleaseError("Опубликованный draft manifest не совпадает.")
    run(
        "gh",
        "release",
        "edit",
        data["tag"],
        "--repo",
        REPOSITORY,
        "--draft=false",
        "--latest=" + ("true" if data["channel"] == "stable" else "false"),
    )
    from language_review.release_transport import fetch_bundle

    with tempfile.TemporaryDirectory(prefix="language-review-public-readback-") as temp:
        fetch_bundle(Path(temp), data["tag"])
        validate_bundle(Path(temp))
        if digest(Path(temp) / "release.json") != digest(args.candidate / "release.json"):
            raise ReleaseError("Публичный manifest после публикации не совпадает.")
    print("GitHub Release опубликован; автоматическое перемещение тегов запрещено.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    candidate = sub.add_parser("prepare")
    candidate.add_argument("--version", required=True)
    candidate.add_argument("--channel", choices=("stable", "prerelease"), required=True)
    candidate.add_argument("--commit", required=True)
    candidate.add_argument("--previous", required=True, help="Последний stable tag или none")
    candidate.add_argument("--notes", type=Path, required=True)
    candidate.add_argument("--output", type=Path, required=True)
    publication = sub.add_parser("publish")
    publication.add_argument("--candidate", type=Path, required=True)
    publication.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args)
        else:
            publish(args)
    except ReleaseError as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()
