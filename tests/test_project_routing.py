from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import JsonServer
from support.check_helpers import llama_responder
from test_chatgpt import MemoryStore, Stream, sse
from test_project_config import project

from language_review import chatgpt_http
from language_review.chatgpt_auth import ChatGptAuth
from language_review.check import check_paths
from language_review.check_report import check_document
from language_review.config import load_config
from language_review.engine_chatgpt import ChatGptProvider
from language_review.engine_llm import IMMUTABLE_INSTRUCTIONS, LlmEngine, effective_prompts
from language_review.project_config import FILENAME, load_project
from language_review.review_report import review_document
from language_review.review_verify import load_review

POLICY = """schema_version: 1
default_profile: source
profiles:
  source:
    engines: [llm]
    analysis: {mode: deep, strategies: [technical], verify: true, concurrency: 2}
    llm: {provider: chatgpt, model: gpt-6-luna, reasoning_effort: medium}
    prompts:
      primary: {text: 'Основной поиск для исходников.'}
  docs:
    engines: [llm]
    analysis: {mode: deep, strategies: [technical], verify: true, concurrency: 2}
    llm: {provider: chatgpt, model: gpt-6.1-sol, reasoning_effort: high}
prompts:
  primary: primary.md
  strategies: {technical: technical.md}
  verifier: verifier.md
path_rules:
  - {path: 'docs/**', profile: docs}
  - {path: 'src/**', profile: source}
scan:
  exclude_patterns: ['primary.md', 'technical.md', 'verifier.md']
"""


def inputs(root: Path, marker: str) -> Path:
    project(root, POLICY)
    (root / ".git").mkdir()
    for kind in ("docs", "src"):
        (root / kind).mkdir()
        for index in range(2):
            (root / kind / f"file{index}.md").write_text(
                f"{marker} {kind} {index}: ошыбка в my_token {{0}}.\n", encoding="utf-8"
            )
    for name in ("primary", "technical", "verifier"):
        (root / f"{name}.md").write_text(f"Инструкция {name} проекта {marker}.", encoding="utf-8")
    return root


@pytest.fixture
def responses(monkeypatch: Any) -> list[dict[str, Any]]:
    from language_review import engine_chatgpt

    calls: list[dict[str, Any]] = []
    lock = threading.Lock()

    class Opener:
        def open(self, request: Any, **options: Any) -> Stream:
            payload = json.loads(request.data)
            with lock:
                calls.append(payload)
            user = json.loads(payload["input"][0]["content"])
            if "hypotheses" in user:
                value = {
                    "decisions": [
                        {
                            "id": item["id"],
                            "excerpt": item["excerpt"],
                            "decision": "confirmed",
                            "evidence": "Синтетическая цитата сверена.",
                        }
                        for item in user["hypotheses"]
                    ]
                }
            else:
                value = {
                    "findings": [
                        {
                            "item": item["id"],
                            "category": "spelling",
                            "excerpt": "ошыбка",
                            "replacement": "ошибка",
                            "explanation": "Опечатка.",
                            "confidence": "high",
                        }
                        for item in user["items"]
                    ]
                }
            return Stream(
                sse(
                    {"type": "response.output_text.delta", "delta": json.dumps(value)},
                    {
                        "type": "response.completed",
                        "response": {
                            "status": "completed",
                            "model": payload["model"],
                            "reasoning": payload["reasoning"],
                        },
                    },
                )
            )

    monkeypatch.setattr(chatgpt_http, "_OPENER", Opener())
    monkeypatch.setattr(
        engine_chatgpt,
        "cloud_json",
        lambda *a, **kw: {
            "models": [
                {"slug": model, "display_name": model, "visibility": "list"}
                for model in ("gpt-6-luna", "gpt-6.1-sol")
            ]
        },
    )
    return calls


def factory(name: str, settings: Any) -> LlmEngine:
    assert name == "llm"
    return LlmEngine(settings, ChatGptProvider(settings, ChatGptAuth(MemoryStore())))


def test_two_repositories_path_profiles_real_transport_repeated_and_parallel(
    tmp_path: Path, isolated_appdata: Path, responses: Any
) -> None:
    first, second = inputs(tmp_path / "first", "Первый"), inputs(tmp_path / "second", "Второй")
    before = {
        path: path.read_bytes()
        for root in (first, second)
        for path in root.rglob("*")
        if path.is_file()
    }
    environment = load_config()

    def run(paths: list[str]) -> Any:
        return check_paths(
            paths,
            environment,
            check_overrides={"llm.chatgpt.allow_cloud": True},
            engine_factory=factory,
        )

    combined = run([str(first), str(second)])
    assert len(combined.engines) == 4
    reversed_result = run([str(second), str(first)])
    with ThreadPoolExecutor(max_workers=2) as pool:
        separate = list(pool.map(lambda path: run([str(path)]), (first, second)))
    for result in (combined, reversed_result, *separate):
        assert result.status == "findings"
        for document in result.documents:
            if not document.units:
                continue
            policy = document.configuration["policy"]
            is_docs = document.extract.path.startswith("docs/")
            assert policy["profile"] == ("docs" if is_docs else "source")
            assert policy["path_rule"] == ("docs/**" if is_docs else "src/**")
            assert policy["model"] == ("gpt-6.1-sol" if is_docs else "gpt-6-luna")
            assert policy["reasoning_effort"] == ("high" if is_docs else "medium")
            assert document.findings and all(
                item.candidates[0].raw.engine_excerpt == "ошыбка" for item in document.findings
            )
        for engine in result.engines:
            assert engine.details["analysis"]["units_remaining"] == 0
            assert {
                row["pass"]
                for row in engine.details["analysis"]["passes"]
                if row["kind"] == "search"
            } == {"primary", "technical"}
    assert responses
    for sent in responses:
        user = json.loads(sent["input"][0]["content"])
        serialized = json.dumps(user, ensure_ascii=False)
        docs = "docs" in serialized
        assert sent["model"] == ("gpt-6.1-sol" if docs else "gpt-6-luna")
        assert sent["reasoning"]["effort"] == ("high" if docs else "medium")
        assert "my_token" not in serialized and "{0}" not in serialized
        assert IMMUTABLE_INSTRUCTIONS in sent["instructions"]
        first_project = "Первый" in serialized
        other_marker = "Второй" if first_project else "Первый"
        assert other_marker not in sent["instructions"]
    assert all(path.read_bytes() == content for path, content in before.items())
    public = json.dumps(check_document(combined), ensure_ascii=False)
    assert "Инструкция primary" not in public and str(tmp_path) not in public
    saved = tmp_path / "review.json"
    saved.write_text(json.dumps(review_document(combined)), encoding="utf-8")
    assert load_review(saved)["schema_version"] == "1.1"

    def by_text(result: Any) -> Any:
        return {
            doc.units[0].views["llm"].text: doc.configuration["policy"]["prompts"]
            for doc in result.documents
            if doc.units
        }

    assert by_text(combined) == by_text(reversed_result)


@pytest.mark.parametrize("name", ["primary", "technical", "verifier"])
def test_external_prompt_changes_actual_instructions_and_hash(
    tmp_path: Path, isolated_appdata: Path, responses: Any, name: str
) -> None:
    root = inputs(tmp_path / "repo", "Первый")
    target = root / "docs/file0.md"
    environment = load_config()
    first = check_paths(
        [str(target)],
        environment,
        check_overrides={"llm.chatgpt.allow_cloud": True},
        engine_factory=factory,
    )
    original_hash = first.documents[0].configuration["policy"]["prompts"]
    assert any(f"Инструкция {name} проекта Первый." in sent["instructions"] for sent in responses)
    responses.clear()
    (root / f"{name}.md").write_text("Изменённая инструкция.", encoding="utf-8")
    second = check_paths(
        [str(target)],
        environment,
        check_overrides={"llm.chatgpt.allow_cloud": True},
        engine_factory=factory,
    )
    new_hash = second.documents[0].configuration["policy"]["prompts"]
    old = original_hash["strategies"][name] if name == "technical" else original_hash[name]
    new = new_hash["strategies"][name] if name == "technical" else new_hash[name]
    assert old["sha256"] != new["sha256"]
    assert any("Изменённая инструкция." in sent["instructions"] for sent in responses)


def test_project_cloud_selection_without_consent_sends_zero_requests(
    tmp_path: Path, isolated_appdata: Path, responses: Any
) -> None:
    root = inputs(tmp_path / "repo", "Первый")
    result = check_paths([str(root / "docs/file0.md")], load_config(), engine_factory=factory)
    assert result.status == "unavailable" and responses == []
    assert "cloud_consent_required" in result.engines[0].diagnostics


def test_unavailable_selected_model_is_not_replaced(
    tmp_path: Path, isolated_appdata: Path, responses: Any, monkeypatch: Any
) -> None:
    from language_review import engine_chatgpt

    root = inputs(tmp_path / "repo", "Первый")
    monkeypatch.setattr(
        engine_chatgpt,
        "cloud_json",
        lambda *a, **kw: {
            "models": [{"slug": "gpt-6-luna", "display_name": "Luna", "visibility": "list"}]
        },
    )
    result = check_paths(
        [str(root / "docs/file0.md")],
        load_config(),
        check_overrides={"llm.chatgpt.allow_cloud": True},
        engine_factory=factory,
    )
    assert result.status == "unavailable" and responses == []
    assert "selected_model_unavailable" in result.engines[0].diagnostics


def test_inline_null_resets_inherited_override_to_builtin(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    root = inputs(tmp_path / "repo", "Первый")
    path = root / FILENAME
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("Основной поиск для исходников.", "Основной поиск для исходников.")
        .replace(
            "primary: {text: 'Основной поиск для исходников.'}",
            "primary: null\n      strategies: {technical: null}\n      verifier: null",
        ),
        encoding="utf-8",
    )
    policy = load_project(load_config(), root)
    settings, _ = policy.check_settings_for_profile("source")
    assert (
        settings.prompt_primary is None
        and settings.prompt_strategies == {}
        and settings.prompt_verifier is None
    )
    assert effective_prompts(settings)["primary"]["source"] == "встроенный промпт"


@pytest.mark.parametrize("provider", ["llama_cpp", "ollama"])
def test_local_transports_receive_project_prompts_and_keep_invalid_json_failure(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer, provider: str
) -> None:
    root = project(
        tmp_path / "repo",
        f"""schema_version: 1
default_profile: local
profiles:
  local:
    engines: [llm]
    llm: {{provider: {provider}, model: synthetic-model}}
    prompts:
      primary: {{text: 'Проектная языковая инструкция.'}}
""",
    )
    source = root / "text.md"
    source.write_text("Тут ошыбка в my_token.", encoding="utf-8")
    before = source.read_bytes()
    base = llama_responder('{"findings": []}')

    def respond(request: Any) -> Any:
        if request.path == "/api/version":
            return 200, {"version": "synthetic"}
        if request.path == "/v1/models":
            return 200, {"data": [{"id": "synthetic-model"}]}
        if request.path == "/api/tags":
            return 200, {"models": [{"name": "synthetic-model"}]}
        if request.path == "/api/show":
            return 200, {}
        if request.path == "/api/chat":
            return 200, {"message": {"content": '{"findings": [null]}'}, "done": True}
        if request.path == "/v1/chat/completions":
            return 200, {
                "choices": [
                    {"message": {"content": '{"findings": [null]}'}, "finish_reason": "stop"}
                ]
            }
        return base(request)

    json_server.responder = respond
    environment = load_config()
    environment = replace(
        environment,
        check=replace(
            environment.check,
            llm_llama_cpp_endpoint=json_server.url,
            llm_ollama_endpoint=json_server.url,
        ),
    )
    result = check_paths([str(source)], environment)
    assert result.status == "unavailable"
    assert "llm_schema_violation" in result.engines[0].diagnostics
    inference = [
        req for req in json_server.requests if req.path in {"/api/chat", "/v1/chat/completions"}
    ]
    assert len(inference) == 1
    assert "Проектная языковая инструкция." in inference[0].body["messages"][0]["content"]
    assert IMMUTABLE_INSTRUCTIONS in inference[0].body["messages"][0]["content"]
    assert inference[0].body["model"] == "synthetic-model"
    assert source.read_bytes() == before


def test_memory_bindings_and_cloud_memory_consent_stay_separate(
    tmp_path: Path, isolated_appdata: Path, responses: Any, monkeypatch: Any
) -> None:
    from language_review.memory.pipeline import MemorySession

    roots = [
        inputs(tmp_path / name, marker)
        for name, marker in (("first", "Первый"), ("second", "Второй"))
    ]
    bindings = []

    class Session:
        def prepare(self, units: Any) -> None:
            for unit in units:
                unit.memory_context = [{"text": "Секретный контекст памяти.", "state": "accepted"}]

        def finish(self, result: Any) -> None:
            pass

    def opened(settings: Any, extract: Any, options: Any, **kw: Any) -> Any:
        bindings.append(settings.projects)
        return Session()

    monkeypatch.setattr(MemorySession, "open", opened)
    check_paths(
        [str(root) for root in roots],
        load_config(),
        check_overrides={"llm.chatgpt.allow_cloud": True},
        engine_factory=factory,
    )
    assert len(set(bindings[0].values())) == 2
    assert responses and all(
        "Секретный контекст памяти" not in json.dumps(row, ensure_ascii=False) for row in responses
    )
    assert not load_config().check.llm_chatgpt_allow_memory


def test_profile_budgets_are_independent_and_never_claim_clean(
    tmp_path: Path, isolated_appdata: Path, responses: Any
) -> None:
    root = inputs(tmp_path / "repo", "Первый")
    policy_path = root / FILENAME
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8").replace(
            "concurrency: 2}", "concurrency: 2, max_requests: 1}", 1
        ),
        encoding="utf-8",
    )
    result = check_paths(
        [str(root)],
        load_config(),
        check_overrides={"llm.chatgpt.allow_cloud": True},
        engine_factory=factory,
    )
    assert result.status == "unavailable"
    source_calls = [row for row in responses if row["model"] == "gpt-6-luna"]
    docs_calls = [row for row in responses if row["model"] == "gpt-6.1-sol"]
    assert len(source_calls) == 1 and len(docs_calls) > 1
    source_run = next(run for run in result.engines if run.details["model"] == "gpt-6-luna")
    docs_run = next(run for run in result.engines if run.details["model"] == "gpt-6.1-sol")
    assert source_run.details["analysis"]["stop_reason"] == "analysis_request_budget"
    assert docs_run.details["analysis"]["units_remaining"] == 0


def test_evaluator_capture_keeps_yaml_prompts_and_repeated_engine_timings(
    tmp_path: Path, isolated_appdata: Path, json_server: JsonServer
) -> None:
    import copy

    from support.evaluation_helpers import make_corpus

    from language_review.evaluation.comparison import compare
    from language_review.evaluation.live import capture

    root = project(
        tmp_path / "policy",
        """schema_version: 1
default_profile: local
profiles:
  local:
    engines: [llm]
    llm: {provider: llama_cpp, model: stub-model}
    prompts:
      primary: instructions.md
prompts:
  primary: instructions.md
""",
    )
    (root / "instructions.md").write_text("Инструкция только для capture.", encoding="utf-8")
    environment = isolated_appdata / "language-review/config.toml"
    environment.parent.mkdir(parents=True)
    environment.write_text(
        f'[check.llm.llama_cpp]\nendpoint = "{json_server.url}"\n', encoding="utf-8"
    )
    initial_policy = (root / FILENAME).read_bytes()
    base_response = llama_responder('{"findings": []}')

    def respond(request: Any) -> Any:
        if request.path == "/v1/chat/completions":
            (root / FILENAME).write_bytes(initial_policy + "\n# Изменено после экспорта\n".encode())
        return base_response(request)

    json_server.responder = respond
    output = tmp_path / "capture"
    result = capture(
        make_corpus("Обычный русский текст."), "dev", ["llm"], root / FILENAME, output, "a" * 40
    )
    assert result["repetitions"] == 1
    report = load_review(output / "report-1.json")
    sent = [req for req in json_server.requests if req.path == "/v1/chat/completions"]
    assert sent and all(
        "Инструкция только для capture." in req.body["messages"][0]["content"] for req in sent
    )
    manifest = json.loads((output / "run-1.json").read_text(encoding="utf-8"))
    import hashlib

    assert manifest["configuration_sha256"] == hashlib.sha256(initial_policy).hexdigest()
    policies = manifest["parameters"]["document_policies"]
    assert policies and all(
        value["prompts"]["primary"]["source"].endswith("instructions.md")
        for value in policies.values()
    )
    assert all(
        doc["configuration"]["policy"]["profile"] == "local"
        for doc in report["analysis"]["documents"]
    )
    baseline = json.loads((output / "evaluation-1.json").read_text(encoding="utf-8"))
    candidate = copy.deepcopy(baseline)
    first = candidate["cost"]["engines"][0]
    candidate["cost"]["engines"].append({**first, "elapsed_ms": 25.0})
    assert compare(baseline, candidate)["time"]["engine_elapsed_ms.llm"]["delta"] == pytest.approx(
        25.0
    )


@pytest.mark.parametrize(
    "strategy", ["grammar", "spelling", "punctuation", "technical", "meaning", "english"]
)
def test_each_strategy_override_reaches_transport(
    tmp_path: Path, isolated_appdata: Path, responses: Any, strategy: str
) -> None:
    root = inputs(tmp_path / "repo", "Первый")
    policy = root / FILENAME
    text = (
        policy.read_text(encoding="utf-8")
        .replace("strategies: [technical]", f"strategies: [{strategy}]")
        .replace("{technical: technical.md}", f"{{{strategy}: technical.md}}")
    )
    policy.write_text(text, encoding="utf-8")
    result = check_paths(
        [str(root / "docs/file0.md")],
        load_config(),
        check_overrides={"llm.chatgpt.allow_cloud": True},
        engine_factory=factory,
    )
    assert result.status == "findings"
    assert any("Инструкция technical проекта Первый." in call["instructions"] for call in responses)
    assert list(result.documents[0].configuration["policy"]["prompts"]["strategies"]) == [strategy]
