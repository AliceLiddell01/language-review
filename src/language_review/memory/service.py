"""Нативное развёртывание и сопровождение Windows worker предметной памяти."""

from __future__ import annotations

import argparse
import contextlib
import csv
import ctypes
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from language_review.distribution import remove_activation_scripts

DEFAULT_EMBEDDING_REVISION = "85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1"
TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
CREDENTIAL = "database.credential.dpapi"
LEGACY_ASSETS = {
    "connect-memory.ps1": "dc49f5daa53a87068230e1316126bc07304ca0a8",
    "run-memory-service.ps1": "c9c9ed35bb0db83e6d96f0e6583d89c1fccea296",
}
DATABASE_ENVIRONMENT = (
    "LANGUAGE_REVIEW_DB_PASSWORD",
    "LANGUAGE_REVIEW_MEMORY_DSN",
)


class ServiceError(ValueError):
    """Диагностируемый отказ без секретов и содержимого subprocess."""


def require_windows() -> None:
    if os.name != "nt":
        raise ServiceError("Требуется Windows.")


def safe_path(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ServiceError("Управляемый путь не должен проходить через ссылку или junction.")


def atomic(path: Path, data: bytes) -> None:
    safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def load_deployment(runtime: Path) -> dict[str, Any]:
    safe_path(runtime / "deployment.json")
    try:
        result = json.loads((runtime / "deployment.json").read_text("utf-8"))
    except OSError, ValueError:
        raise ServiceError("Состояние установки отсутствует или повреждено.") from None
    if not isinstance(result, dict):
        raise ServiceError("Некорректное состояние установки.")
    return result


def dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    require_windows()

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
        raise ServiceError("Windows DPAPI не разрешила доступ к учётным данным.")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def current_user() -> tuple[str, str]:
    result = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise ServiceError("Не удалось определить текущего пользователя Windows.")
    # SID состоит из ASCII; имя пользователя не используется при построении команд.
    rows = list(csv.reader(io.StringIO(result.stdout.decode("oem"))))
    if len(rows) != 1 or len(rows[0]) != 2 or not re.fullmatch(r"S-1-[0-9-]+", rows[0][1]):
        raise ServiceError("Не удалось определить SID текущего пользователя Windows.")
    return rows[0][0], rows[0][1]


def protect_directory(runtime: Path) -> None:
    """Закрытая наследуемая DACL: только текущий пользователь и SYSTEM."""
    require_windows()
    safe_path(runtime)
    _, sid = current_user()
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p()
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
    security.SetFileSecurityW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_void_p]
    security.SetFileSecurityW.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = f"D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)"
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        descriptor, 1, ctypes.byref(pointer), None
    ):
        raise ServiceError("Не удалось построить закрытые права каталога runtime.")
    try:
        if not security.SetFileSecurityW(str(runtime), 0x80000004, pointer):
            raise ServiceError("Не удалось установить закрытые права каталога runtime.")
    finally:
        kernel.LocalFree(pointer)


def write_password(runtime: Path, password: str) -> None:
    if not password or "\x00" in password:
        raise ServiceError("Некорректные учётные данные БД.")
    safe_path(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    protect_directory(runtime)
    atomic(runtime / CREDENTIAL, dpapi(password.encode("utf-8")))


def read_password(runtime: Path) -> str:
    safe_path(runtime / CREDENTIAL)
    if (runtime / CREDENTIAL).exists():
        try:
            return dpapi((runtime / CREDENTIAL).read_bytes(), decrypt=True).decode("utf-8")
        except OSError, UnicodeError:
            raise ServiceError("Учётные данные БД повреждены.") from None
    legacy = runtime / "database.credential.xml"
    safe_path(legacy)
    try:
        if legacy.stat().st_size > 1_000_000:
            raise ValueError
        root = ET.fromstring(legacy.read_bytes())
        names = {node.attrib.get("N"): node for node in root.iter()}
        username = names.get("UserName")
        secret = names.get("Password")
        if (
            username is None
            or username.text != "language_review"
            or secret is None
            or secret.tag.rsplit("}", 1)[-1] != "SS"
            or not secret.text
        ):
            raise ValueError
        password = dpapi(bytes.fromhex(secret.text), decrypt=True).decode("utf-16-le")
    except OSError, ValueError, UnicodeError, ET.ParseError:
        raise ServiceError("Нужны исходные DPAPI-учётные данные БД этого пользователя.") from None
    # Старый ciphertext сохраняется; ни временных plaintext файлов, ни PowerShell.
    write_password(runtime, password)
    return password


def database_environment(
    runtime: Path, deployment: Mapping[str, Any] | None = None
) -> dict[str, str]:
    deployment = deployment if deployment is not None else load_deployment(runtime)
    password = read_password(runtime)
    port = int(deployment["port"])
    environment = dict(os.environ)
    environment.update(
        {
            "LANGUAGE_REVIEW_DB_PASSWORD": password,
            "LANGUAGE_REVIEW_DB_PORT": str(port),
            "LANGUAGE_REVIEW_MEMORY_DSN": (
                f"postgresql://language_review:{urllib.parse.quote(password, safe='')}"
                f"@127.0.0.1:{port}/language_review"
            ),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


def scrub(text: str, environment: Mapping[str, str]) -> str:
    for key in DATABASE_ENVIRONMENT:
        value = environment.get(key)
        if value:
            text = text.replace(value, "[СКРЫТО]")
            text = text.replace(urllib.parse.quote(value, safe=""), "[СКРЫТО]")
    return text


def process_creation_time(pid: int) -> int:
    """FILETIME рождения из handle защищает операторскую проверку от повторного PID."""
    require_windows()
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.c_void_p] * 4)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ServiceError("Процесс установки недоступен.")
    values = [ctypes.c_uint64() for _ in range(4)]
    try:
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
            raise ServiceError("Не удалось проверить время рождения процесса установки.")
        return int(values[0].value)
    finally:
        kernel.CloseHandle(handle)


@contextlib.contextmanager
def install_lock(runtime: Path) -> Iterator[None]:
    """Mutex не создаёт каталог и сериализует проверки/публикацию одной установки."""
    require_windows()
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    identity = hashlib.sha256(str(runtime).casefold().encode()).hexdigest()
    handle = kernel.CreateMutexW(None, False, "Local\\LanguageReview-MemoryInstall-" + identity)
    if not handle:
        raise ServiceError("Не удалось заблокировать установку worker.")
    acquired = False
    try:
        acquired = kernel.WaitForSingleObject(handle, 0) in {0, 0x80}
        if not acquired:
            raise ServiceError("Другая операция установки этого worker уже выполняется.")
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


class Scheduler:
    def __init__(self, runtime: Path, project: str) -> None:
        self.runtime = runtime
        self.name = "LanguageReview-Memory-" + project
        self.description = "Language Review memory: " + str(runtime)

    def _call(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        require_windows()
        return subprocess.run(
            ["schtasks.exe", *arguments],
            capture_output=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    @staticmethod
    def _parse_xml(output: bytes) -> ET.Element:
        try:
            return ET.fromstring(output)
        except ET.ParseError:
            if output.startswith((b"\xff\xfe", b"\xfe\xff")):
                text = output.decode("utf-16")
            elif output[:64].count(b"\0") > 8:
                sample = output[:64]
                encoding = (
                    "utf-16-le"
                    if sample[1::2].count(b"\0") > sample[0::2].count(b"\0")
                    else "utf-16-be"
                )
                text = output.decode(encoding)
            else:
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                codepage = kernel.GetConsoleOutputCP() or kernel.GetACP()
                text = output.decode(f"cp{codepage}")
            text = re.sub(r"^\s*<\?xml[^?]*\?>\s*", "", text, count=1)
            return ET.fromstring(text)

    def query(self) -> dict[str, Any] | None:
        result = self._call("/Query", "/TN", self.name, "/XML", "/HRESULT")
        if result.returncode in {1, 0x80070002, -2147024894}:
            return None
        if result.returncode:
            raise ServiceError("Не удалось прочитать задачу Windows Scheduler.")
        try:
            root = self._parse_xml(result.stdout)
        except ET.ParseError, UnicodeError:
            raise ServiceError("Windows Scheduler вернул повреждённое описание задачи.") from None
        namespace = {"t": TASK_NAMESPACE}
        description = root.findtext("t:RegistrationInfo/t:Description", namespaces=namespace)
        if description != self.description:
            raise ServiceError("Задача с этим именем принадлежит другой установке.")
        return {
            "xml": ET.tostring(root, encoding="unicode"),
            "description": description,
            "enabled": root.findtext("t:Settings/t:Enabled", namespaces=namespace) != "false",
            "actions": [
                {
                    "execute": node.findtext("t:Command", namespaces=namespace),
                    "arguments": node.findtext("t:Arguments", namespaces=namespace),
                    "working_directory": node.findtext("t:WorkingDirectory", namespaces=namespace),
                }
                for node in root.findall("t:Actions/t:Exec", namespaces=namespace)
            ],
        }

    def register(self, xml: str) -> None:
        self.query()
        with tempfile.TemporaryDirectory(prefix="lr-scheduler-") as directory:
            path = Path(directory) / "task.xml"
            path.write_text(xml, encoding="utf-16")
            result = self._call("/Create", "/TN", self.name, "/XML", str(path), "/F")
        if result.returncode:
            raise ServiceError("Не удалось зарегистрировать задачу Windows Scheduler.")

    def _owned(self, *arguments: str) -> None:
        if self.query() is None:
            raise ServiceError("Задача установки отсутствует.")
        if self._call(*arguments, "/TN", self.name).returncode:
            raise ServiceError("Не удалось изменить состояние задачи Windows Scheduler.")

    def start(self) -> None:
        self._owned("/Run")

    def stop(self) -> None:
        if self.query() is not None:
            self._owned("/End")
        # .pyw владеет Job Object: завершение task закрывает весь принадлежащий ей job.
        deadline = time.monotonic() + 30
        while service_locked(self.runtime):
            if time.monotonic() >= deadline:
                raise ServiceError("Supervisor установки не остановился за 30 секунд.")
            time.sleep(0.25)

    def enable(self, enabled: bool) -> None:
        self._owned("/Change", "/Enable" if enabled else "/Disable")

    def remove(self) -> None:
        if self.query() is not None:
            self._owned("/Delete", "/F")


def task_xml(runtime: Path, pythonw: Path, sid: str) -> str:
    ET.register_namespace("", TASK_NAMESPACE)
    root = ET.Element("Task", {"xmlns": TASK_NAMESPACE, "version": "1.4"})

    def append(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
        node = ET.SubElement(parent, tag)
        node.text = text
        return node

    registration = append(root, "RegistrationInfo")
    append(registration, "Description", "Language Review memory: " + str(runtime))
    triggers = append(root, "Triggers")
    logon = append(triggers, "LogonTrigger")
    append(logon, "Enabled", "true")
    append(logon, "UserId", sid)
    periodic = append(triggers, "TimeTrigger")
    repetition = append(periodic, "Repetition")
    append(repetition, "Interval", "PT1M")
    append(repetition, "StopAtDurationEnd", "false")
    append(periodic, "StartBoundary", (datetime.now() + timedelta(minutes=1)).isoformat())
    append(periodic, "Enabled", "true")
    principals = append(root, "Principals")
    principal = ET.SubElement(principals, "Principal", {"id": "Author"})
    append(principal, "UserId", sid)
    append(principal, "LogonType", "InteractiveToken")
    append(principal, "RunLevel", "LeastPrivilege")
    settings = append(root, "Settings")
    for tag, text in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("StartWhenAvailable", "true"),
        ("Enabled", "true"),
        ("ExecutionTimeLimit", "PT0S"),
    ):
        append(settings, tag, text)
    restart = append(settings, "RestartOnFailure")
    append(restart, "Interval", "PT1M")
    append(restart, "Count", "999")
    actions = ET.SubElement(root, "Actions", {"Context": "Author"})
    execute = append(actions, "Exec")
    append(execute, "Command", str(pythonw))
    append(
        execute,
        "Arguments",
        subprocess.list2cmdline(["-I", str(runtime / "run-memory-service.pyw")]),
    )
    append(execute, "WorkingDirectory", str(runtime))
    return ET.tostring(root, encoding="unicode")


@contextlib.contextmanager
def service_lock(runtime: Path) -> Iterator[bool]:
    require_windows()
    import msvcrt

    path = runtime / "logs/service.lock"
    safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def service_locked(runtime: Path) -> bool:
    if not (runtime / "logs/service.lock").exists():
        return False
    try:
        with service_lock(runtime) as acquired:
            return not acquired
    except OSError:
        # Старый supervisor держал файл с FileShare.None, запрещавшим даже open.
        return True


def models() -> list[dict[str, Any]]:
    with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as response:
        value = json.load(response)
    return list(value["models"])


def database_state(containers: list[dict[str, Any]]) -> str:
    if not containers:
        return "absent"
    states = {row.get("State") for row in containers}
    # Unhealthy/restarting не доказывают остановку: recovery запрещён.
    if "running" in states:
        return "running"
    if states <= {"exited", "dead", "created", "stopped"}:
        return "stopped"
    return "unknown"


class Supervisor:
    def __init__(
        self,
        runtime: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        database_factory: Callable[..., Any] | None = None,
        models_loader: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.runtime = runtime
        self.deployment = load_deployment(runtime)
        self.environment = database_environment(runtime, self.deployment)
        self.logs = runtime / "logs"
        safe_path(self.logs)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.runner = runner or self.run_process
        if database_factory is None:
            from language_review.memory.database import Database

            database_factory = Database
        self.database = database_factory(
            runtime / "compose.memory.yml",
            self.deployment["project"],
            environment=self.environment,
            docker=self.deployment["docker"],
        )
        self.models_loader = models_loader or models
        self.retry_delay = 5
        self.created_at = process_creation_time(os.getpid())

    def write_state(self, record: dict[str, Any]) -> None:
        record = {
            **record,
            "at": datetime.now(UTC).isoformat(),
            "supervisor_pid": os.getpid(),
            "release": self.deployment["release"],
            "supervisor_created_at": self.created_at,
        }
        content = scrub(json_bytes(record).decode("utf-8"), self.environment).encode("utf-8")
        atomic(self.logs / "service-state.json", content)
        if record["status"] != "idle":
            log = self.logs / "service.jsonl"
            safe_path(log)
            if log.exists() and log.stat().st_size > 5 * 1024 * 1024:
                log.replace(log.with_suffix(".jsonl.1"))
            with log.open("ab") as stream:
                stream.write(content)

    def run_process(
        self, command: list[str], *, env: dict[str, str], timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        # communicate одновременно читает оба pipe и предотвращает взаимную блокировку.
        with subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ) as process:
            try:
                output, error = process.communicate(timeout=timeout)
            except BaseException:
                process.kill()
                process.communicate()
                raise
            return subprocess.CompletedProcess(command, process.returncode, output, error)

    def cycle(self) -> int:
        delay = 5
        try:
            available = self.models_loader()
            if not all(
                any(
                    row.get("name") == self.deployment[name + "_model"]
                    and row.get("digest") == self.deployment[name + "_revision"]
                    for row in available
                )
                for name in ("llm", "embedding")
            ):
                self.write_state(
                    {"status": "waiting", "reason": "configured_model_unavailable_or_changed"}
                )
                return delay
            command = [
                self.deployment["python"],
                "-I",
                "-m",
                "language_review",
                "--config",
                self.deployment["config"],
                "memory",
                "work",
                "--all-projects",
                "--limit",
                str(self.deployment["worker_limit"]),
            ]
            invocation = self.runner(command, env=self.environment, timeout=None)
            atomic(
                self.logs / "worker.stderr.log",
                scrub(invocation.stderr, self.environment).encode("utf-8"),
            )
            code = invocation.returncode
            if code in {0, 4}:
                result = json.loads(invocation.stdout)
                status = (
                    result["status"]
                    if result.get("jobs") or result.get("index", {}).get("indexed") or code == 4
                    else "idle"
                )
                self.retry_delay = 5
                self.write_state({"status": status, "exit_code": code, "result": result})
            else:
                match = (
                    re.search(r"\b(memory_[a-z0-9_]+)\b", invocation.stderr) if code == 6 else None
                )
                reason = match[1] if match else "worker_failed"
                database = None
                if reason == "memory_database_failed":
                    from language_review.memory.database import DatabaseError

                    try:
                        containers = self.database.status()
                        database = database_state(containers)
                    except DatabaseError as exc:
                        database = (
                            "docker_unavailable" if exc.code == "docker_unavailable" else exc.code
                        )
                if database in {"stopped", "absent"}:
                    self.retry_delay = 5
                    self.write_state(
                        {
                            "status": "waiting",
                            "exit_code": code,
                            "reason": "database_unavailable",
                            "database": database,
                            "recovery": "compose_up",
                        }
                    )
                    self.database.up(wait_timeout=30)
                elif database == "docker_unavailable":
                    self.retry_delay = 5
                    self.write_state({"status": "waiting", "exit_code": code, "reason": database})
                else:
                    delay = self.retry_delay
                    self.retry_delay = min(300, delay * 2)
                    record: dict[str, Any] = {
                        "status": "error",
                        "exit_code": code,
                        "reason": reason,
                        "retry_in_seconds": delay,
                    }
                    if database:
                        record["database"] = database
                    self.write_state(record)
        except Exception as exc:
            self.write_state({"status": "waiting", "error_type": type(exc).__name__})
        return delay

    def run(self) -> int:
        with service_lock(self.runtime) as acquired:
            if not acquired:
                return 0
            self.write_state({"status": "starting"})
            while True:
                time.sleep(self.cycle())


def _checked(
    command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        details = scrub(result.stdout + result.stderr, env if env is not None else os.environ)
        if details:
            print(details, file=sys.stderr, end="" if details.endswith("\n") else "\n")
        raise ServiceError(
            f"Операция {Path(command[0]).name} завершилась с кодом {result.returncode}."
        )
    return result.stdout.strip()


def _validate_options(
    source: Path,
    runtime: Path,
    project: str,
    port: int,
    llm_model: str,
    embedding_model: str,
    embedding_revision: str,
    embedding_dimensions: int,
) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,60}", project):
        raise ServiceError(f"Некорректный Project: {project}")
    if not 1024 <= port <= 65535 or not 1 <= embedding_dimensions <= 2000:
        raise ServiceError("Некорректные port или embedding dimensions.")
    if not re.fullmatch(r"[a-zA-Z0-9._:/-]+", llm_model):
        raise ServiceError("Некорректная LLM model.")
    if not re.fullmatch(r"(?:embeddinggemma|qwen3-embedding)[a-zA-Z0-9._:/-]*", embedding_model):
        raise ServiceError("Некорректная embedding model.")
    if not re.fullmatch(r"[a-f0-9]{64}", embedding_revision):
        raise ServiceError("Нужен точный SHA-256 embedding revision.")
    if (
        '"' in str(runtime)
        or runtime == Path(runtime.anchor)
        or runtime == Path.home()
        or runtime == source
        or runtime.is_relative_to(source)
    ):
        raise ServiceError(
            "RuntimeDirectory должен находиться вне checkout и не содержать кавычек."
        )
    safe_path(runtime)
    if any((part / ".git").exists() for part in (runtime, *runtime.parents)):
        raise ServiceError("RuntimeDirectory должен находиться вне checkout.")
    if (
        runtime.exists()
        and any(runtime.iterdir())
        and not any(
            (runtime / name).is_file()
            for name in ("deployment.json", CREDENTIAL, "database.credential.xml")
        )
    ):
        raise ServiceError(
            "Отказ менять права непустого каталога, не принадлежащего этой установке."
        )


def wait_ready(runtime: Path, release: Path, after: datetime, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            record = json.loads((runtime / "logs/service-state.json").read_text("utf-8"))
            own = (
                record.get("release") == str(release)
                and datetime.fromisoformat(record["at"]) > after
            )
        except OSError, ValueError, KeyError, TypeError:
            own = False
        if own and record.get("status") in {"idle", "processed", "partial"}:
            return dict(record)
        if own and record.get("status") == "error":
            raise ServiceError("Новый release сообщил об ошибке worker; см. состояние и журналы.")
        time.sleep(0.5)
    raise ServiceError("Задача не подтвердила готовность; см. состояние и журналы.")


def install_service(
    *,
    release_directory: Path | None = None,
    runtime_directory: Path | None = None,
    project: str = "language-review-memory",
    port: int = 54329,
    llm_model: str = "qwen3.5:4b",
    embedding_model: str = "embeddinggemma:300m",
    embedding_revision: str = DEFAULT_EMBEDDING_REVISION,
    embedding_dimensions: int = 768,
    uv: str | None = None,
) -> dict[str, Any]:
    runtime = (runtime_directory or Path.home() / ".language-review/memory").absolute()
    safe_path(runtime)
    runtime = runtime.resolve()
    source = (release_directory or Path(__file__).resolve().parents[3]).absolute()
    safe_path(source)
    source = source.resolve()
    _validate_options(
        source,
        runtime,
        project,
        port,
        llm_model,
        embedding_model,
        embedding_revision,
        embedding_dimensions,
    )
    with install_lock(runtime):
        return _install_service(
            release_directory=source if release_directory else None,
            runtime_directory=runtime,
            project=project,
            port=port,
            llm_model=llm_model,
            embedding_model=embedding_model,
            embedding_revision=embedding_revision,
            embedding_dimensions=embedding_dimensions,
            uv=uv,
        )


def _install_service(
    *,
    release_directory: Path | None = None,
    runtime_directory: Path | None = None,
    project: str = "language-review-memory",
    port: int = 54329,
    llm_model: str = "qwen3.5:4b",
    embedding_model: str = "embeddinggemma:300m",
    embedding_revision: str = DEFAULT_EMBEDDING_REVISION,
    embedding_dimensions: int = 768,
    uv: str | None = None,
) -> dict[str, Any]:
    source = (release_directory or Path(__file__).resolve().parents[3]).absolute()
    runtime = (runtime_directory or Path.home() / ".language-review/memory").absolute()
    _validate_options(
        source,
        runtime,
        project,
        port,
        llm_model,
        embedding_model,
        embedding_revision,
        embedding_dimensions,
    )
    require_windows()
    from language_review.memory.database import Database
    from language_review.release_contract import (
        ReleaseError,
        digest,
        unpack_wheelhouse,
        validate_bundle,
    )

    try:
        manifest = validate_bundle(source) if release_directory else None
    except ReleaseError as exc:
        raise ServiceError(str(exc)) from None
    docker = shutil.which("docker")
    uv = uv or shutil.which("uv")
    if not docker or not uv:
        raise ServiceError("Требуются установленные Docker Desktop и uv.")
    old = load_deployment(runtime) if (runtime / "deployment.json").exists() else None
    if old and (old.get("project") != project or old.get("port") != port):
        raise ServiceError("Существующая установка использует другие project/port.")
    scheduler = Scheduler(runtime, project)
    previous_task = scheduler.query()
    legacy_files: list[str] = []
    for name, expected in LEGACY_ASSETS.items():
        path = runtime / name
        safe_path(path)
        if path.exists():
            content = path.read_bytes().replace(b"\r\n", b"\n")
            header = f"blob {len(content)}\0".encode()
            if hashlib.sha1(header + content).hexdigest() != expected:
                raise ServiceError("Старый служебный файл изменён пользователем: " + name)
            legacy_files.append(name)
    _checked([docker, "info", "--format", "{{.ServerVersion}}"])
    available = models()
    llms = [row for row in available if row.get("name") == llm_model]
    embeddings = [row for row in available if row.get("name") == embedding_model]
    if len(llms) != 1 or len(embeddings) != 1 or embeddings[0].get("digest") != embedding_revision:
        raise ServiceError("Нужны настроенные локальные модели с точной embedding revision.")
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/embed",
        data=json_bytes(
            {"model": embedding_model, "input": ["Проверка русского технического текста CLI"]}
        ),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        probe = json.load(response).get("embeddings", [])
    if len(probe) != 1 or len(probe[0]) != embedding_dimensions:
        raise ServiceError("Размерность embedding не совпадает с установленной моделью.")
    runtime.mkdir(parents=True, exist_ok=True)
    protect_directory(runtime)
    if not (runtime / CREDENTIAL).exists() and not (runtime / "database.credential.xml").exists():
        volume = _checked(
            [
                docker,
                "volume",
                "ls",
                "--filter",
                f"name=^{project}_memory-data$",
                "--format",
                "{{.Name}}",
            ]
        )
        if volume:
            raise ServiceError("Для существующего volume нужны его исходные учётные данные.")
        write_password(runtime, secrets.token_hex(32))
    environment = database_environment(runtime, {"port": port})
    release = runtime / "releases" / uuid.uuid4().hex
    safe_path(release)
    release.mkdir(parents=True)
    requirements = release / "requirements.txt"
    if manifest:
        shutil.copyfile(source / "requirements-memory.txt", requirements)
        wheels = list(source.glob("*.whl"))
        if len(wheels) != 1:
            raise ServiceError("Ожидался один релизный wheel.")
        shutil.copyfile(wheels[0], release / wheels[0].name)
    else:
        _checked(
            [
                uv,
                "export",
                "--locked",
                "--no-dev",
                "--extra",
                "memory",
                "--no-emit-project",
                "--output-file",
                str(requirements),
            ],
            cwd=source,
        )
        _checked([uv, "build", "--wheel", "--out-dir", str(release)], cwd=source)
    _checked([uv, "venv", "--python", "3.15", str(release / "venv")])
    remove_activation_scripts(release)
    python = release / "venv/Scripts/python.exe"
    dependency_args: list[str] = []
    if manifest:
        unpack_wheelhouse(source, release / "wheelhouse")
        dependency_args = [
            "--no-index",
            "--find-links",
            str(release / "wheelhouse"),
            "--only-binary",
            ":all:",
        ]
    _checked(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            *dependency_args,
            "-r",
            str(requirements),
        ]
    )
    wheels = list(release.glob("*.whl"))
    if len(wheels) != 1:
        raise ServiceError("Ожидался ровно один собранный wheel.")
    _checked([uv, "pip", "install", "--python", str(python), "--no-deps", str(wheels[0])])
    remove_activation_scripts(release)
    pythonw = Path(
        _checked(
            [
                str(python),
                "-I",
                "-c",
                "import pathlib,sys; "
                "print(pathlib.Path(sys._base_executable).with_name('pythonw.exe'))",
            ]
        )
    )
    if not pythonw.is_file():
        raise ServiceError("Не найден базовый безоконный интерпретатор Python.")
    source_sha = (
        manifest["commit"] if manifest else _checked(["git", "rev-parse", "HEAD"], cwd=source)
    )
    source_dirty = (
        False
        if manifest
        else bool(
            _checked(["git", "status", "--porcelain=v1", "--untracked-files=normal"], cwd=source)
        )
    )
    config = (
        '[check]\nengines = ["llm"]\n[check.llm]\nprovider = "ollama"\ntimeout_seconds = 120\n'
        f"[check.llm.ollama]\nmodel = {json.dumps(llm_model)}\n[memory]\nenabled = true\n"
        'dsn_env = "LANGUAGE_REVIEW_MEMORY_DSN"\nrecall_mode = "hybrid"\n'
        f"embedding_model = {json.dumps(embedding_model)}\n"
        f"embedding_revision = {json.dumps(embedding_revision)}\n"
        f"embedding_dimensions = {embedding_dimensions}\n"
    ).encode()
    atomic(release / "worker.toml", config)
    deployment = {
        "schema": "language-review.memory-service/1",
        "status": "candidate",
        "project": project,
        "port": port,
        "task": scheduler.name,
        "python": str(python),
        "docker": docker,
        "config": str(runtime / "worker.toml"),
        "release": str(release),
        "worker_limit": 8,
        "source_sha": source_sha,
        "source_dirty": source_dirty,
        "wheel_sha256": digest(wheels[0]),
        "lock_sha256": manifest["lock_sha256"] if manifest else digest(source / "uv.lock"),
        "llm_model": llm_model,
        "llm_revision": llms[0]["digest"],
        "embedding_model": embedding_model,
        "embedding_revision": embedding_revision,
    }
    scripts = source if manifest else source / "scripts"
    published = (
        "compose.memory.yml",
        "run-memory-service.pyw",
        "worker.toml",
        "deployment.json",
        *legacy_files,
    )
    previous: dict[str, bytes | None] = {}
    migrated = False
    snapshotted = False
    fault = (
        os.environ.get("LANGUAGE_REVIEW_TEST_INSTALL_FAULT")
        if re.fullmatch(r"lr-service-test-[0-9a-f]{12}", project)
        else None
    )
    try:
        if previous_task:
            scheduler.stop()
            scheduler.enable(False)
        database = Database(
            source / "compose.memory.yml", project, environment=environment, docker=docker
        )
        database.up(wait_timeout=120)
        migrated = True
        for operation in ("migrate", "health"):
            _checked(
                [
                    str(python),
                    "-I",
                    "-m",
                    "language_review",
                    "--config",
                    str(release / "worker.toml"),
                    "memory",
                    operation,
                ],
                env=environment,
            )
        for name in published:
            safe_path(runtime / name)
            previous[name] = (runtime / name).read_bytes() if (runtime / name).exists() else None
        snapshotted = True
        atomic(runtime / "compose.memory.yml", (source / "compose.memory.yml").read_bytes())
        atomic(
            runtime / "run-memory-service.pyw", (scripts / "run-memory-service.pyw").read_bytes()
        )
        atomic(
            runtime / "worker.toml",
            config + (b"\ninvalid = = toml\n" if fault == "readiness" else b""),
        )
        atomic(runtime / "deployment.json", json_bytes(deployment))
        if fault == "register":
            raise ServiceError("Контролируемый сбой на этапе регистрации задачи.")
        scheduler.register(task_xml(runtime, pythonw, current_user()[1]))
        scheduler.enable(True)
        if fault == "start":
            raise ServiceError("Контролируемый сбой на этапе запуска задачи.")
        started = datetime.now(UTC)
        scheduler.start()
        wait_ready(runtime, release, started, 8 * 120 + 180)
        deployment["status"] = "installed"
        for name in legacy_files:
            (runtime / name).unlink()
        atomic(runtime / "deployment.json", json_bytes(deployment))
        return deployment
    except Exception as failure:
        errors: list[str] = []
        try:
            scheduler.stop()
        except Exception:
            errors.append("остановка новой задачи")
        if snapshotted:
            for name, data in previous.items():
                try:
                    if data is None:
                        (runtime / name).unlink(missing_ok=True)
                    else:
                        atomic(runtime / name, data)
                except Exception:
                    errors.append("восстановление файла " + name)
        try:
            if previous_task:
                scheduler.register(previous_task["xml"])
                scheduler.enable(previous_task["enabled"])
                if previous_task["enabled"]:
                    scheduler.start()
            else:
                scheduler.remove()
        except Exception:
            errors.append("восстановление задачи")
        if migrated and old and not errors:
            try:
                _checked(
                    [
                        old["python"],
                        "-I",
                        "-m",
                        "language_review",
                        "--config",
                        old["config"],
                        "memory",
                        "health",
                    ],
                    env=environment,
                )
            except Exception:
                errors.append(
                    "прежний release не принимает текущую схему БД; "
                    "миграции автоматически не откатываются"
                )
        prefix = "Обновление не выполнено" if old or previous_task else "Установка не выполнена"
        result = (
            "Откат выполнен не полностью: " + "; ".join(errors)
            if errors
            else (
                "Прежние файлы runtime и задача восстановлены."
                if old or previous_task
                else "Задача и опубликованные файлы удалены; "
                "учётные данные, volume и данные БД сохранены."
            )
        )
        reason = str(failure) if isinstance(failure, ServiceError) else type(failure).__name__
        raise ServiceError(
            f"{prefix}: {reason} {result} Кандидат оставлен для диагностики: {release}."
        ) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    install = commands.add_parser("install", help="установить или обновить отдельный worker")
    install.add_argument("--release-directory", type=Path)
    install.add_argument("--runtime-directory", type=Path)
    install.add_argument("--project", default="language-review-memory")
    install.add_argument("--port", type=int, default=54329)
    install.add_argument("--llm-model", default="qwen3.5:4b")
    install.add_argument("--embedding-model", default="embeddinggemma:300m")
    install.add_argument("--embedding-revision", default=DEFAULT_EMBEDDING_REVISION)
    install.add_argument("--embedding-dimensions", type=int, default=768)
    install.add_argument("--uv")
    for name in ("supervise", "connect"):
        command = commands.add_parser(name)
        command.add_argument("--runtime-directory", type=Path, required=True)
        if name == "connect":
            command.add_argument("command", nargs=argparse.REMAINDER)
    worker = commands.add_parser("start-worker")
    worker.add_argument("--config", type=Path, required=True)
    worker.add_argument("--project", type=uuid.UUID, required=True)
    worker.add_argument("--log-directory", type=Path, required=True)
    arguments = vars(parser.parse_args(argv))
    operation = arguments.pop("operation")
    try:
        if operation == "install":
            print(json.dumps(install_service(**arguments), ensure_ascii=False))
            return 0
        require_windows()
        if operation == "supervise":
            return Supervisor(arguments["runtime_directory"].absolute()).run()
        if operation == "connect":
            command = arguments["command"]
            if command and command[0] == "--":
                command = command[1:]
            if not command:
                raise ServiceError("После -- требуется исполняемая программа и её аргументы.")
            return subprocess.run(
                command, env=database_environment(arguments["runtime_directory"]), check=False
            ).returncode
        config = arguments["config"].resolve(strict=True)
        logs = arguments["log_directory"].absolute()
        safe_path(logs)
        logs.mkdir(parents=True, exist_ok=True)
        with (
            (logs / "worker.stdout.log").open("ab") as output,
            (logs / "worker.stderr.log").open("ab") as error,
        ):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "language_review",
                    "--config",
                    str(config),
                    "memory",
                    "work",
                    "--project",
                    str(arguments["project"]),
                    "--watch",
                ],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=error,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        print(process.pid)
        return 0
    except ServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Служебная операция не выполнена: {type(exc).__name__}.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
