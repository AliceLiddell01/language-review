"""Пользовательское DPAPI-хранилище и межпроцессная сериализация сессий Windows."""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from language_review.chatgpt_http import CloudError


def dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    if sys.platform != "win32":
        raise CloudError("chatgpt_windows_storage_required")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    method = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    method.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(Blob),
    ]
    method.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise CloudError("chatgpt_storage_protection_failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class CredentialStore:
    def __init__(self, directory: Path | None = None) -> None:
        base = os.environ.get("LOCALAPPDATA")
        if directory is None and not base:
            raise CloudError("chatgpt_windows_storage_required")
        self.directory = directory or Path(str(base)) / "LanguageReview" / "chatgpt"
        if not self.directory.is_absolute():
            raise CloudError("chatgpt_storage_path_invalid")
        self.path = self.directory / "accounts.dpapi"

    def _prepare(self) -> None:
        for part in (self.directory, *self.directory.parents):
            if part.is_symlink() or part.is_junction():
                raise CloudError("chatgpt_storage_link_refused")
            if (part / ".git").exists():
                raise CloudError("chatgpt_storage_inside_repository")
        self.directory.mkdir(parents=True, exist_ok=True)
        for name in ("accounts.dpapi", "session.lock"):
            path = self.directory / name
            if path.is_symlink() or path.is_junction():
                raise CloudError("chatgpt_storage_link_refused")

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        if sys.platform != "win32":
            raise CloudError("chatgpt_windows_storage_required")
        import msvcrt

        self._prepare()
        with (self.directory / "session.lock").open("a+b") as lock:
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            deadline = time.monotonic() + 60
            while True:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise CloudError("chatgpt_session_busy") from None
                    time.sleep(0.05)
            try:
                if self.path.exists():
                    if self.path.stat().st_size > 2_000_000:
                        raise CloudError("chatgpt_storage_invalid")
                    try:
                        database = json.loads(dpapi(self.path.read_bytes(), decrypt=True))
                    except ValueError, UnicodeError:
                        raise CloudError("chatgpt_storage_invalid") from None
                    if (
                        not isinstance(database, dict)
                        or database.get("version") != 1
                        or not isinstance(database.get("accounts"), dict)
                        or not isinstance(database.get("host_id"), str)
                        or not database["host_id"].startswith("urn:uuid:")
                        or "active" not in database
                        or (
                            database["active"] is not None
                            and (
                                not isinstance(database["active"], str)
                                or database["active"] not in database["accounts"]
                            )
                        )
                    ):
                        raise CloudError("chatgpt_storage_invalid")
                    for label, row in database["accounts"].items():
                        if (
                            not isinstance(label, str)
                            or not isinstance(row, dict)
                            or row.get("state") not in ("signed_in", "signed_out")
                            or not isinstance(row.get("client_id"), str)
                            or not isinstance(row.get("subject"), str)
                        ):
                            raise CloudError("chatgpt_storage_invalid")
                        if row["state"] == "signed_in" and (
                            not isinstance(row.get("scopes"), list)
                            or not all(isinstance(scope, str) for scope in row["scopes"])
                            or type(row.get("expires_at")) not in {int, float}
                        ):
                            raise CloudError("chatgpt_storage_invalid")
                else:
                    database = {
                        "version": 1,
                        "host_id": "urn:uuid:" + str(uuid4()),
                        "active": None,
                        "accounts": {},
                    }
                yield database
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    def save(self, database: dict[str, Any]) -> None:
        protected = dpapi(json.dumps(database, ensure_ascii=False).encode())
        descriptor, name = tempfile.mkstemp(prefix="pending-", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(protected)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
