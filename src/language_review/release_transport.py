"""HTTPS только к GitHub, без выполнения данных из release metadata."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from language_review.release_contract import (
    REPOSITORY,
    SHA,
    ReleaseError,
    read_json,
    validate_manifest,
    version_key,
)

HOSTS = {
    "api.github.com",
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}


def check_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in HOSTS
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise ReleaseError("Отказ загрузки: требуется HTTPS с разрешённого GitHub host.")


class GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, target: Path, expected: str | None = None, limit: int = 100_000_000) -> None:
    check_url(url)
    partial = target.with_name(target.name + ".partial")
    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "language-review-installer",
                "Accept": "application/vnd.github+json"
                if url.startswith("https://api.github.com/")
                else "application/octet-stream",
            },
        )
        with (
            urllib.request.build_opener(GitHubRedirect()).open(request, timeout=45) as response,
            partial.open("xb") as stream,
        ):
            total = 0
            checksum = hashlib.sha256()
            while block := response.read(64 * 1024):
                total += len(block)
                if total > limit:
                    raise ReleaseError("Превышен размер загрузки.")
                checksum.update(block)
                stream.write(block)
        if expected is not None and checksum.hexdigest() != expected:
            raise ReleaseError("SHA-256 загруженного asset не совпадает.")
        partial.replace(target)
    except (OSError, urllib.error.URLError) as exc:
        raise ReleaseError("GitHub недоступен или загрузка прервана; установка сохранена.") from exc
    finally:
        partial.unlink(missing_ok=True)


def api(path: str) -> Any:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="language-review-api-") as directory:
        target = Path(directory) / "response.json"
        download("https://api.github.com/repos/" + REPOSITORY + "/" + path, target, limit=8_000_000)
        return json.loads(target.read_text("utf-8"))


def published(tag: str | None = None, *, prerelease: bool = False) -> dict[str, Any]:
    if tag:
        if not tag.startswith("v"):
            raise ReleaseError("Укажите полный tag вида vX.Y.Z.")
        version_key(tag.removeprefix("v"))
        release = api("releases/tags/" + tag)
    elif prerelease:
        releases = [r for r in api("releases?per_page=100") if not r["draft"]]
        if not releases:
            raise ReleaseError("Опубликованных релизов нет.")
        release = max(releases, key=lambda r: version_key(r["tag_name"].removeprefix("v")))
    else:
        release = api("releases/latest")
    if (
        release.get("draft")
        or not release.get("published_at")
        or (release.get("prerelease") and not (tag or prerelease))
    ):
        raise ReleaseError("Требуется опубликованный релиз разрешённого канала.")
    version_key(release["tag_name"].removeprefix("v"))
    return dict(release)


def tag_commit(tag: str) -> str:
    version_key(tag.removeprefix("v"))
    obj = api("git/ref/tags/" + tag)["object"]
    for _ in range(6):
        if not SHA.fullmatch(str(obj.get("sha", ""))):
            raise ReleaseError("Некорректный объект тега GitHub.")
        if obj.get("type") == "commit":
            return str(obj["sha"])
        if obj.get("type") != "tag":
            break
        obj = api("git/tags/" + obj["sha"])["object"]
    raise ReleaseError("Тег не разрешается в точный commit.")


def fetch_bundle(
    directory: Path, tag: str | None = None, *, prerelease: bool = False
) -> dict[str, Any]:
    release = published(tag, prerelease=prerelease)
    assets = {item["name"]: item for item in release["assets"]}
    if len(assets) != len(release["assets"]) or "release.json" not in assets:
        raise ReleaseError("Неоднозначный или неполный список assets GitHub.")
    item = assets["release.json"]
    checksum = item.get("digest", "")
    if not checksum.startswith("sha256:"):
        raise ReleaseError("GitHub API не предоставил digest manifest.")
    prefix = f"https://github.com/{REPOSITORY}/releases/download/{release['tag_name']}/"
    if item["browser_download_url"] != prefix + "release.json":
        raise ReleaseError("Manifest принадлежит другому release URL.")
    download(
        item["browser_download_url"], directory / "release.json", checksum[7:], limit=1_000_000
    )
    data = read_json(directory / "release.json")
    validate_manifest(data)
    commit = tag_commit(data["tag"])
    if (
        data["tag"] != release["tag_name"]
        or data["commit"] != commit
        or release["prerelease"] != (data["channel"] == "prerelease")
    ):
        raise ReleaseError("Тег, SHA или канал GitHub не совпадает с manifest.")
    for name, expected in data["assets"].items():
        asset = assets.get(name, {})
        if (
            asset.get("browser_download_url") != prefix + name
            or asset.get("digest") != "sha256:" + expected["sha256"]
            or asset.get("size") != expected["size"]
        ):
            raise ReleaseError("GitHub asset не соответствует manifest: " + name)
        download(prefix + name, directory / name, expected["sha256"], expected["size"])
    return dict(data)
