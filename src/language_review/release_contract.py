"""Проверяемая идентичность поставки; модуль bootstrap использует только stdlib."""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any

REPOSITORY = "AliceLiddell01/language-review"
SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(alpha|beta|rc)\.([1-9]\d*))?\Z")
SHA = re.compile(r"[a-f0-9]{40}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\Z")
RUNTIME_ASSETS = (
    "install-memory-service.ps1",
    "connect-memory.ps1",
    "run-memory-service.ps1",
    "run-memory-service.pyw",
    "compose.memory.yml",
)


class ReleaseError(ValueError):
    """Отказ установки или выпуска с безопасным для журналов объяснением."""


def version_key(version: str) -> tuple[int, int, int, int, int]:
    match = SEMVER.fullmatch(version)
    if not match:
        raise ReleaseError("Версия требует X.Y.Z или X.Y.Z-{alpha,beta,rc}.N без ведущих нулей.")
    major, minor, patch, pre, number = match.groups()
    return (
        int(major),
        int(minor),
        int(patch),
        {"alpha": 0, "beta": 1, "rc": 2, None: 3}[pre],
        int(number or 0),
    )


def package_version(version: str) -> str:
    version_key(version)
    return re.sub(
        r"-(alpha|beta|rc)\.", lambda m: {"alpha": "a", "beta": "b", "rc": "rc"}[m[1]], version
    )


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReleaseError("Повторный ключ JSON.")
            result[key] = value
        return result

    try:
        result = json.loads(path.read_text("utf-8"), object_pairs_hook=unique)
    except (OSError, ValueError) as exc:
        raise ReleaseError("Недоступный или повреждённый JSON поставки.") from exc
    if not isinstance(result, dict):
        raise ReleaseError("Ожидался объект JSON поставки.")
    return result


def validate_manifest(data: dict[str, Any]) -> None:
    if data.get("schema") != "language-review.release/1" or data.get("repository") != REPOSITORY:
        raise ReleaseError("Неизвестная схема или издатель поставки.")
    version = data.get("version")
    if not isinstance(version, str):
        raise ReleaseError("Отсутствует версия поставки.")
    version_key(version)
    if data.get("package_version") != package_version(version) or data.get("tag") != "v" + version:
        raise ReleaseError("Версии тега, пакета и manifest не совпадают.")
    if data.get("channel") != ("prerelease" if "-" in version else "stable"):
        raise ReleaseError("Канал не соответствует версии.")
    if not SHA.fullmatch(str(data.get("commit", ""))):
        raise ReleaseError("Нужен полный commit SHA.")
    if data.get("python") != "3.15" or data.get("skill_contract") != 1:
        raise ReleaseError("Несовместимый Python или контракт skill.")
    assets = data.get("assets")
    if not isinstance(assets, dict) or not assets:
        raise ReleaseError("Нет проверяемых assets.")
    for name, entry in assets.items():
        if not ASSET.fullmatch(name) or ".." in name or not isinstance(entry, dict):
            raise ReleaseError("Недопустимое имя asset.")
        if (
            not HASH.fullmatch(str(entry.get("sha256", "")))
            or type(entry.get("size")) is not int
            or not 0 < entry["size"] <= 100_000_000
        ):
            raise ReleaseError("Недопустимый hash или размер asset.")
    required = {
        "wheelhouse.zip",
        "install.ps1",
        "release-notes.md",
        "requirements-core.txt",
        "requirements-chatgpt.txt",
        "requirements-memory.txt",
        "requirements-all.txt",
        *RUNTIME_ASSETS,
    }
    if (
        not required <= assets.keys()
        or sum(n.endswith(".whl") for n in assets) != 1
        or sum(n.endswith(".tar.gz") for n in assets) != 1
    ):
        raise ReleaseError("Релизный комплект неполон.")


def validate_bundle(directory: Path) -> dict[str, Any]:
    data = read_json(directory / "release.json")
    validate_manifest(data)
    for name, entry in data["assets"].items():
        path = directory / name
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != entry["size"]
            or digest(path) != entry["sha256"]
        ):
            raise ReleaseError(
                "Asset отсутствует, загружен частично или не совпадает с SHA-256: " + name
            )
    wheel = next(directory.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.infolist():
            if (
                ".." in member.filename.split("/")
                or "\\" in member.filename
                or member.filename.startswith("/")
                or ":" in member.filename
            ):
                raise ReleaseError("Wheel содержит небезопасный путь.")
        names = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ReleaseError("Некорректные метаданные wheel.")
        metadata = BytesParser().parsebytes(archive.read(names[0]))
        identity = json.loads(archive.read("language_review/release_identity.json"))
        skill = archive.read("language_review/distribution_assets/SKILL.md").decode("utf-8")
    if (
        metadata["Name"] != "language-review"
        or metadata["Version"] != data["package_version"]
        or identity != {k: data[k] for k in ("version", "package_version", "commit", "tag")}
        or f'product-version: "{data["package_version"]}"' not in skill
    ):
        raise ReleaseError("Wheel не соответствует релизной идентичности.")
    with tarfile.open(next(directory.glob("*.tar.gz"))) as archive:
        members = [m for m in archive.getmembers() if m.name.endswith("/PKG-INFO")]
        if len(members) != 1:
            raise ReleaseError("Некорректные метаданные sdist.")
        stream = archive.extractfile(members[0])
        if (
            stream is None
            or BytesParser().parsebytes(stream.read())["Version"] != data["package_version"]
        ):
            raise ReleaseError("Версия sdist не совпадает с релизом.")
    expected_files = {*data["assets"], "release.json", "SHA256SUMS"}
    if any(path.name not in expected_files or not path.is_file() for path in directory.iterdir()):
        raise ReleaseError("Комплект содержит посторонние файлы.")
    return data


def unpack_wheelhouse(bundle: Path, target: Path) -> None:
    """Извлекает только плоский набор wheel, без ссылок, traversal и zip bomb."""
    with zipfile.ZipFile(bundle / "wheelhouse.zip") as archive:
        total = 0
        names: set[str] = set()
        for entry in archive.infolist():
            total += entry.file_size
            if (
                not ASSET.fullmatch(entry.filename)
                or not entry.filename.endswith(".whl")
                or ".." in entry.filename
                or entry.filename.casefold() in names
                or total > 500_000_000
                or entry.file_size > 100_000_000
                or (entry.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ReleaseError("Небезопасный архив зависимостей.")
            names.add(entry.filename.casefold())
        if not names:
            raise ReleaseError("Архив зависимостей пуст.")
        target.mkdir()
        for entry in archive.infolist():
            with archive.open(entry) as source, (target / entry.filename).open("xb") as destination:
                import shutil

                shutil.copyfileobj(source, destination)
