from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _load_script(name: str) -> Any:
    path = Path(__file__).parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_hindsight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    api_version: str = "1.0.0",
    container_identifier: str = "a" * 64,
    benchmark_exit: int = 0,
    cleanup_exit: int = 0,
) -> tuple[Any, list[list[str]]]:
    experiment = _load_script("experiment-hindsight.py")
    calls: list[list[str]] = []

    class TemporaryDirectory:
        def __init__(self, **_kwargs: Any) -> None:
            self.path = tmp_path

        def __enter__(self) -> str:
            return str(self.path)

        def __exit__(self, *_args: Any) -> None:
            return None

    def run(arguments: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(arguments)
        if arguments[:2] == ["docker", "run"]:
            cid_file = Path(arguments[arguments.index("--cidfile") + 1])
            cid_file.write_text(container_identifier, encoding="ascii")
            return SimpleNamespace(returncode=0)
        if arguments[:2] == ["uv", "run"]:
            return SimpleNamespace(returncode=benchmark_exit)
        if arguments[:3] == ["docker", "rm", "--force"]:
            return SimpleNamespace(returncode=cleanup_exit)
        raise AssertionError(f"Неожиданный вызов: {arguments}")

    monkeypatch.setattr(experiment, "reserve_port", lambda: 43123)
    monkeypatch.setattr(
        experiment, "api_description", lambda _url: {"info": {"version": api_version}}
    )
    monkeypatch.setattr(experiment.tempfile, "TemporaryDirectory", TemporaryDirectory)
    monkeypatch.setattr(experiment.subprocess, "run", run)
    return experiment, calls


def test_hindsight_cleanup_targets_only_container_id_from_current_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    container = "a" * 64
    experiment, calls = _stub_hindsight(monkeypatch, tmp_path, container_identifier=container)

    output = tmp_path / "benchmark.json"
    assert experiment.run_experiment(output) == 0

    assert calls[0][:4] == ["docker", "run", "-d", "--cidfile"]
    assert "127.0.0.1:43123:8888" in calls[0]
    assert experiment.IMAGE in calls[0]
    assert calls[1][0:2] == ["uv", "run"]
    assert calls[1][calls[1].index("--extra") + 1] == "memory"
    assert calls[1][-2:] == ["--output", str(output)]
    assert calls[2] == ["docker", "rm", "--force", container]
    assert "Артефакты:" in capsys.readouterr().out


def test_hindsight_failed_benchmark_still_removes_container_and_preserves_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    experiment, calls = _stub_hindsight(monkeypatch, tmp_path, benchmark_exit=7)

    result = experiment.run_experiment(tmp_path / "benchmark.json")

    assert result == 7
    assert calls[-1] == ["docker", "rm", "--force", "a" * 64]
    assert "Benchmark Hindsight не выполнен." in capsys.readouterr().err


def test_hindsight_version_mismatch_still_removes_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    experiment, calls = _stub_hindsight(monkeypatch, tmp_path, api_version="0.9.0")

    with pytest.raises(experiment.ExperimentError, match="Неожиданная версия API"):
        experiment.run_experiment(tmp_path / "benchmark.json")

    assert calls[-1] == ["docker", "rm", "--force", "a" * 64]


def test_hindsight_cleanup_failure_returns_failure_after_successful_benchmark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    experiment, calls = _stub_hindsight(monkeypatch, tmp_path, cleanup_exit=1)

    assert experiment.run_experiment(tmp_path / "benchmark.json") == 1

    assert calls[-1] == ["docker", "rm", "--force", "a" * 64]
    assert "Не удалось удалить контейнер текущего эксперимента." in capsys.readouterr().err


def test_hindsight_rejects_unowned_container_identifier(tmp_path: Path) -> None:
    experiment = _load_script("experiment-hindsight.py")
    cid_file = tmp_path / "container.id"
    cid_file.write_text("container-name", encoding="ascii")

    assert experiment.container_id(cid_file) is None


def test_hindsight_does_not_remove_container_for_unowned_identifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    experiment, calls = _stub_hindsight(
        monkeypatch, tmp_path, container_identifier="container-name"
    )

    assert experiment.run_experiment(tmp_path / "benchmark.json") == 0

    assert not any(call[:3] == ["docker", "rm", "--force"] for call in calls)


def test_pilot_parser_keeps_engine_and_limit_contract() -> None:
    pilot = _load_script("reproduce-reference-pilot.py")

    arguments = pilot.parser().parse_args(
        [
            "--destination",
            "artifacts",
            "--engine",
            "vale",
            "--engine",
            "languagetool",
            "--pair-limit",
            "1",
            "--book-limit",
            "10000",
        ]
    )

    assert arguments.engine == ["vale", "languagetool"]
    assert arguments.pair_limit == 1
    assert arguments.book_limit == 10000
    assert arguments.code_limit == 24
    with pytest.raises(SystemExit):
        pilot.parser().parse_args(["--destination", "artifacts", "--code-limit", "0"])


def test_pilot_destination_must_be_new_and_outside_checkout(tmp_path: Path) -> None:
    pilot = _load_script("reproduce-reference-pilot.py")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = tmp_path / "results"

    with pytest.raises(pilot.PilotError, match="вне checkout"):
        pilot.prepare_destination(checkout / "results", checkout)

    outside.mkdir()
    with pytest.raises(pilot.PilotError, match="Нужен новый каталог"):
        pilot.prepare_destination(outside, checkout)

    assert pilot.prepare_destination(outside / "new-run", checkout) == outside / "new-run"


def test_pilot_cache_root_must_be_outside_checkout_before_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pilot = _load_script("reproduce-reference-pilot.py")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    cache = checkout / "cache"
    cache.mkdir()
    target = tmp_path / "results"
    arguments = pilot.parser().parse_args(
        ["--destination", str(target), "--cache-root", str(cache)]
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(pilot, "command", lambda args, **_kwargs: commands.append(args))

    with pytest.raises(pilot.PilotError, match="CacheRoot должен находиться вне checkout"):
        pilot.run_pilot(arguments, checkout)

    assert commands == []
    assert not target.exists()


def test_package_smoke_cleans_workspace_when_uv_cache_discovery_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verifier = _load_script("verify-package.py")
    workspace = tmp_path / "smoke-workspace"
    workspace.mkdir()
    monkeypatch.setattr(verifier, "_make_workspace", lambda: (tmp_path, workspace))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)

    def run(label: str, _arguments: list[str], **_kwargs: Any) -> SimpleNamespace:
        if label == "поиск Python 3.15":
            return SimpleNamespace(stdout=str(Path(__file__).resolve()))
        if label == "определение кэша uv":
            raise verifier.SmokeFailure("Синтетическая ошибка поиска кэша uv.")
        raise AssertionError(f"Неожиданный шаг smoke-проверки: {label}")

    monkeypatch.setattr(verifier, "_run", run)

    with pytest.raises(verifier.SmokeFailure, match="Синтетическая ошибка поиска кэша uv"):
        verifier.verify_package()

    assert not workspace.exists()


def test_package_smoke_removes_workspace_after_failed_temp_path_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verifier = _load_script("verify-package.py")
    temp_base = tmp_path / "system-temp"
    temp_base.mkdir()
    workspace = tmp_path / "outside-temp"
    workspace.mkdir()
    monkeypatch.setattr(verifier.tempfile, "gettempdir", lambda: str(temp_base))
    monkeypatch.setattr(verifier.tempfile, "mkdtemp", lambda **_kwargs: str(workspace))

    with pytest.raises(verifier.SmokeFailure, match="вышел за системный каталог"):
        verifier._make_workspace()

    assert not workspace.exists()


def test_pilot_offline_file_comparison_is_byte_exact(tmp_path: Path) -> None:
    pilot = _load_script("reproduce-reference-pilot.py")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_bytes(b'{"status":"PASS"}\n')
    second.write_bytes(b'{"status":"PASS"}\n')

    pilot.require_equal_files(first, second, "не совпало")
    second.write_bytes(b'{"status":"PASS"}')

    with pytest.raises(pilot.PilotError, match="не совпало"):
        pilot.require_equal_files(first, second, "не совпало")


def test_pilot_roundtrip_comparison_keeps_json_types_distinct() -> None:
    pilot = _load_script("reproduce-reference-pilot.py")

    assert pilot.compact_json({"value": 1}) != pilot.compact_json({"value": True})


def test_pilot_evaluation_captures_stdout_and_accepts_only_declared_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pilot = _load_script("reproduce-reference-pilot.py")
    calls: list[tuple[list[str], Path, Any]] = []

    def run(arguments: list[str], *, cwd: Path, stdout: Any, check: bool) -> SimpleNamespace:
        calls.append((arguments, cwd, stdout))
        if stdout is not None:
            stdout.write(b'{"status":"PARTIAL"}\n')
        return SimpleNamespace(returncode=4)

    monkeypatch.setattr(pilot.subprocess, "run", run)
    output = tmp_path / "capture.json"

    pilot.evaluation(tmp_path, ["run", "corpus.json"], output=output, allowed={4})

    assert output.read_bytes() == b'{"status":"PARTIAL"}\n'
    assert calls[0][0] == ["uv", "run", "--locked", "language-review-eval", "run", "corpus.json"]
    assert calls[0][1] == tmp_path
    assert calls[0][2] is not None
    with pytest.raises(pilot.PilotError, match="кодом 4"):
        pilot.evaluation(tmp_path, ["validate", "corpus.json"])


def test_pilot_orchestrates_imports_capture_and_offline_replays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pilot = _load_script("reproduce-reference-pilot.py")
    repository = tmp_path / "checkout"
    repository.mkdir()
    target = tmp_path / "pilot-artifacts"
    arguments = pilot.parser().parse_args(
        [
            "--destination",
            str(target),
            "--engine",
            "vale",
            "--engine",
            "llm",
            "--pair-limit",
            "2",
            "--book-limit",
            "7",
            "--code-limit",
            "3",
        ]
    )
    commit = "a" * 40
    calls: list[tuple[list[str], Path | None, set[int] | None]] = []
    score_bytes = b'{"status":"PARTIAL"}\n'

    monkeypatch.setattr(pilot, "git_state", lambda _repository: (commit, ""))
    monkeypatch.setattr(pilot, "command", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(
        pilot,
        "command_text",
        lambda command, **_kwargs: (
            "Python 3.15.0" if command[-2:] == ["python", "--version"] else "uv 0.13.1"
        ),
    )

    def fake_evaluation(
        _repository: Path,
        command: list[str],
        *,
        output: Path | None = None,
        allowed: set[int] | None = None,
    ) -> None:
        calls.append((command, output, allowed))
        operation = command[0]
        if operation == "import-source":
            name = command[1]
            bundle = Path(command[command.index("--output") + 1])
            bundle.mkdir()
            corpus = {
                "id": name,
                "version": "source-version",
                "split_policy": {"strategy": "source"},
                "documents": [{"id": name}],
                "cases": [{"id": name}],
            }
            (bundle / "corpus.json").write_text(json.dumps(corpus), encoding="utf-8")
        elif operation == "annotation-export":
            Path(command[command.index("--output") + 1]).mkdir()
        elif operation == "annotation-import":
            source = json.loads(Path(command[1]).read_text("utf-8"))
            Path(command[command.index("--output") + 1]).write_text(
                json.dumps(source), encoding="utf-8"
            )
        elif operation == "freeze-test":
            source = Path(command[1]).read_bytes()
            Path(command[command.index("--output") + 1]).write_bytes(source)
        elif operation == "run":
            run_path = Path(command[command.index("--output") + 1])
            run_path.mkdir()
            (run_path / "report-1.json").write_text("{}", encoding="utf-8")
            (run_path / "run-1.json").write_text("{}", encoding="utf-8")
            (run_path / "evaluation-1.json").write_bytes(score_bytes)
            if output is not None:
                output.write_bytes(b'{"status":"PARTIAL"}\n')
        elif operation == "score":
            Path(command[command.index("--output") + 1]).write_bytes(score_bytes)
        elif operation == "reference-score":
            Path(command[command.index("--output") + 1]).write_bytes(b'{"reference":true}\n')
        elif operation in {"validate", "compare"}:
            if output is not None:
                output.write_bytes(b'{"status":"PASS"}\n')
            elif "--output" in command:
                Path(command[command.index("--output") + 1]).write_bytes(b'{"status":"PASS"}\n')
        else:
            raise AssertionError(f"Unexpected evaluator command: {operation}")

    monkeypatch.setattr(pilot, "evaluation", fake_evaluation)

    result = pilot.run_pilot(arguments, repository)

    assert result == target
    assert json.loads((target / "environment.json").read_text("utf-8"))["product_commit"] == commit
    combined = json.loads((target / "combined-corpus.json").read_text("utf-8"))
    assert len(combined["documents"]) == len(combined["cases"]) == 6
    imports = [call[0] for call in calls if call[0][0] == "import-source"]
    assert [(command[1], command[command.index("--limit") + 1]) for command in imports] == [
        ("rust-book-ru", "7"),
        ("sage-github-ru", "2"),
        ("cubic-spline-ru", "3"),
        ("xnet-ru", "3"),
        ("far-ru", "3"),
        ("tglock-cli-ru", "3"),
    ]
    runs = [call for call in calls if call[0][0] == "run"]
    assert len(runs) == 6
    assert all(call[2] == {4} for call in runs)
    assert all(call[0].count("--engine") == 2 for call in runs)
    assert runs[1][0][1] == str(target / "sage-github-ru-frozen.json")
    rescoring = [call for call in calls if call[0][0] == "score"]
    assert len(rescoring) == 6 and all(call[2] == {4} for call in rescoring)
    references = [call for call in calls if call[0][0] == "reference-score"]
    assert len(references) == 2 and all(call[2] == {4} for call in references)
