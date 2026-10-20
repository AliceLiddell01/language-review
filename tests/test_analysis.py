from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from language_review.chatgpt_http import CloudError
from language_review.check import check_paths
from language_review.check_models import ProbeResult
from language_review.check_report import check_document, check_exit_code
from language_review.config import CHATGPT_EFFORTS, CheckSettings, load_config
from language_review.engine_http import EngineHttpError
from language_review.engine_llm import LlmEngine, ProviderReply
from language_review.errors import ConfigurationError
from language_review.review_report import review_document
from language_review.review_verify import load_review, verify_review


class Provider:
    name = "llama_cpp"
    endpoint = "http://127.0.0.1:1"
    model = "synthetic-model"

    def __init__(self, respond: Callable[[str, dict[str, Any]], Any] | None = None) -> None:
        self.respond = respond
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def probe(self) -> ProbeResult:
        return ProbeResult("available")

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> ProviderReply:
        payload = json.loads(user)
        self.calls.append((system, payload))
        value = self.respond(system, payload) if self.respond else {"findings": []}
        return ProviderReply(value if isinstance(value, str) else json.dumps(value), False, 10, 5)


def finding(
    item: str, excerpt: str = "ошыбка", replacement: str | None = "ошибка", **kw: Any
) -> dict[str, Any]:
    return {
        "item": item,
        "category": "spelling",
        "excerpt": excerpt,
        "replacement": replacement,
        "explanation": "Опечатка.",
        "confidence": "high",
    } | kw


def run(
    tmp_path: Path,
    provider: Provider,
    text: str = "Тут ошыбка.\n\nЭтот текст верен.\n",
    **options: Any,
) -> Any:
    source = tmp_path / "source.md"
    source.write_text(text, encoding="utf-8")
    before = source.read_bytes()
    settings = replace(CheckSettings(engines=("llm",)), **options)
    config = replace(load_config(), check=settings)
    result = check_paths(
        [str(source)], config, engine_factory=lambda name, settings: LlmEngine(settings, provider)
    )
    assert source.read_bytes() == before
    return result


def test_deep_search_includes_clean_units_and_preserves_pass_sources(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    def respond(system: str, payload: dict[str, Any]) -> Any:
        if "Дополнительный поисковый проход" not in system:
            return {"findings": []}
        return {
            "findings": [
                finding(item["id"]) for item in payload["items"] if "ошыбка" in item["text"]
            ]
        }

    provider = Provider(respond)
    result = run(
        tmp_path, provider, analysis_mode="deep", analysis_strategies=("spelling", "grammar")
    )
    assert len(provider.calls) == 3
    assert all(len(payload["items"]) == 2 for _, payload in provider.calls)
    [found] = result.documents[0].findings
    assert len(found.candidates) == 2
    assert {c.raw.rule_id for c in found.candidates} == {
        "ru-technical-review/4:spelling:spelling",
        "ru-technical-review/4:grammar:spelling",
    }
    report = review_document(result)
    assert report["schema_version"] == "1.1"
    assert report["items"][0]["agreement"] == ["llm"]
    ledger = result.engines[0].details["analysis"]
    assert ledger["units_complete"] == 2 and ledger["units_remaining"] == 0
    assert all(
        unit["routing"] == ["all_applicable"] for p in ledger["passes"][1:] for unit in p["units"]
    )
    path = tmp_path / "review.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    assert load_review(path)["analysis"] == report["analysis"]
    assert verify_review(path, {"root-0001": tmp_path / "source.md"})["status"] == "current"


def test_adaptive_routing_uses_observed_uncertainty_and_explains_omissions(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    def respond(system: str, payload: dict[str, Any]) -> Any:
        if "Дополнительный поисковый проход" in system:
            return {"findings": []}
        return {"findings": [finding(payload["items"][0]["id"], confidence="low")]}

    provider = Provider(respond)
    result = run(tmp_path, provider, analysis_mode="adaptive", analysis_strategies=("grammar",))
    assert len(provider.calls[0][1]["items"]) == 2
    assert len(provider.calls[1][1]["items"]) == 1
    assert "ошыбка" in provider.calls[1][1]["items"][0]["text"]
    additional = result.engines[0].details["analysis"]["passes"][1]
    assert additional["units"][0]["routing"] == ["model_uncertainty"]
    assert len(additional["not_selected"]) == 1
    assert additional["not_selected"][0]["reason"] == "adaptive_no_signal"


@pytest.mark.parametrize(
    "budget,reason",
    [
        ({"analysis_max_requests": 1}, "analysis_request_budget"),
        ({"analysis_max_text_chars": 1}, "analysis_text_budget"),
        ({"analysis_max_passes": 1}, "analysis_pass_budget"),
        ({"analysis_timeout_seconds": 0.0000001}, "analysis_time_budget"),
    ],
)
def test_deep_budgets_never_claim_clean(
    tmp_path: Path, isolated_appdata: Path, budget: dict[str, Any], reason: str
) -> None:
    provider = Provider()
    result = run(
        tmp_path, provider, analysis_mode="deep", analysis_strategies=("grammar",), **budget
    )
    assert result.status in {"partial", "unavailable"} and check_exit_code(result) in {4, 5}
    analysis = result.engines[0].details["analysis"]
    assert analysis["units_complete"] == 0 and analysis["units_remaining"] == 2
    assert analysis["chars_remaining"] > 0
    assert reason in result.engines[0].diagnostics
    assert len(provider.calls) <= 1


@pytest.mark.parametrize("decision", ["confirmed", "rejected", "uncertain"])
def test_verifier_keeps_original_hypothesis_and_does_not_rewrite(
    tmp_path: Path, isolated_appdata: Path, decision: str
) -> None:
    def respond(system: str, payload: dict[str, Any]) -> Any:
        if "hypotheses" in payload:
            hypothesis = payload["hypotheses"][0]
            return {
                "decisions": [
                    {
                        "id": hypothesis["id"],
                        "excerpt": hypothesis["excerpt"],
                        "decision": decision,
                        "evidence": "Независимая критическая оценка контекста.",
                    }
                ]
            }
        return {"findings": [finding(payload["items"][0]["id"])]}

    provider = Provider(respond)
    result = run(tmp_path, provider, analysis_verify=True)
    assert result.status == "findings"
    [found] = result.documents[0].findings
    assert found.suggestions[0].engine_replacement == "ошибка"
    verification = result.engines[0].details["analysis"]["passes"][1]
    assert verification["decisions"][0]["decision"] == decision
    assert verification["decisions"][0]["hypothesis"]["replacements"] == ["ошибка"]
    assert verification["decisions"][0]["hypothesis"]["engine"] == "llm"
    assert check_document(result)["suggestions_applied"] is False


def test_verifier_invalid_quote_and_timeout_are_incomplete(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    def respond(system: str, payload: dict[str, Any]) -> Any:
        if "hypotheses" in payload:
            return {
                "decisions": [
                    {
                        "id": "0",
                        "excerpt": "другая цитата",
                        "decision": "confirmed",
                        "evidence": "нет",
                    }
                ]
            }
        return {"findings": [finding(payload["items"][0]["id"])]}

    result = run(tmp_path, Provider(respond), analysis_verify=True)
    assert result.status == "partial"
    assert result.documents[0].findings
    assert "verification_invalid_response" in result.engines[0].diagnostics


@pytest.mark.parametrize("replacement", ["не ошибка", "ошибка 42", "error_code", "ошибка\\n"])
def test_extra_search_keeps_meaning_and_technical_rejections(
    tmp_path: Path, isolated_appdata: Path, replacement: str
) -> None:
    def respond(system: str, payload: dict[str, Any]) -> Any:
        return {"findings": [finding(payload["items"][0]["id"], replacement=replacement)]}

    result = run(
        tmp_path, Provider(respond), analysis_mode="deep", analysis_strategies=("grammar",)
    )
    assert result.documents[0].findings
    assert all(s.status == "rejected" for f in result.documents[0].findings for s in f.suggestions)


def test_wrong_quote_rejected_in_each_pass(tmp_path: Path, isolated_appdata: Path) -> None:
    provider = Provider(
        lambda s, p: {"findings": [finding(p["items"][0]["id"], excerpt="не существующая цитата")]}
    )
    result = run(tmp_path, provider, analysis_mode="deep", analysis_strategies=("spelling",))
    assert not result.documents[0].findings
    assert [r.reason for r in result.documents[0].rejected] == [
        "excerpt_not_found",
        "excerpt_not_found",
    ]
    assert review_document(result)["status"] == "partial"


def test_concurrent_variants_and_same_error_across_documents_are_preserved(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    for name in ("one.md", "two.md"):
        (tmp_path / name).write_text("Эта ошыбка.\n", encoding="utf-8")
    provider = Provider(
        lambda s, p: {
            "findings": [
                finding(item["id"], replacement=r)
                for item in p["items"]
                for r in ("ошибка", "ошибочка")
            ]
        }
    )
    config = replace(
        load_config(),
        check=CheckSettings(
            engines=("llm",), analysis_mode="deep", analysis_strategies=("spelling",)
        ),
    )
    result = check_paths(
        [str(tmp_path)], config, engine_factory=lambda n, s: LlmEngine(s, provider)
    )
    assert len(result.documents) == 2 and all(len(doc.findings) == 2 for doc in result.documents)
    assert len({f.id for d in result.documents for f in d.findings}) == 4
    assert all(len(found.candidates) == 2 for doc in result.documents for found in doc.findings)


def test_transient_retry_counts_actual_calls_once_and_quota_stops_remaining(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    provider = Provider()

    def respond(system: str, payload: dict[str, Any]) -> Any:
        if len(provider.calls) == 1:
            raise EngineHttpError("http_503", "temporary")
        return {"findings": []}

    provider.respond = respond
    result = run(tmp_path, provider, analysis_retries=1)
    assert result.status == "clean" and result.engines[0].requests == 2
    assert result.engines[0].request_failures == 1
    assert result.engines[0].details["analysis"]["retries"] == 1
    provider = Provider()

    def limited(system: str, payload: dict[str, Any]) -> Any:
        raise EngineHttpError("subscription_sharing_usage_limit_exceeded", "quota")

    provider.respond = limited
    result = run(
        tmp_path,
        provider,
        analysis_mode="deep",
        analysis_strategies=("grammar",),
        llm_batch_items=1,
        analysis_retries=3,
    )
    assert len(provider.calls) == 1 and result.engines[0].requests == 1
    assert result.status == "unavailable"


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("rate_limit_exceeded", 429),
        ("rate_limit_exceeded", None),
        ("server_error", 500),
        ("server_error", 503),
    ],
)
def test_cloud_http_status_retries_provider_error_codes(
    tmp_path: Path, isolated_appdata: Path, code: str, status: int | None
) -> None:
    attempts = 0

    def respond(_system: str, _payload: dict[str, Any]) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise CloudError(code, status=status)
        return {"findings": []}

    provider = Provider(respond)
    result = run(tmp_path, provider, text="Тут ошыбка.\n", analysis_retries=1)

    assert result.status == "clean"
    assert len(provider.calls) == 2
    assert result.engines[0].details["analysis"]["retries"] == 1


@pytest.mark.parametrize(("code", "status"), [("invalid_api_key", 401), ("forbidden", 403)])
def test_cloud_auth_status_stops_remaining_requests(
    tmp_path: Path, isolated_appdata: Path, code: str, status: int
) -> None:
    def respond(_system: str, _payload: dict[str, Any]) -> Any:
        raise CloudError(code, status=status)

    provider = Provider(respond)
    result = run(
        tmp_path,
        provider,
        analysis_retries=2,
        analysis_concurrency=1,
    )

    assert len(provider.calls) == 1
    analysis = result.engines[0].details["analysis"]
    assert analysis["requests"] == 1
    assert analysis["retries"] == 0


@pytest.mark.parametrize("allow_memory", [False, True])
def test_existing_memory_recall_obeys_separate_cloud_consent(
    tmp_path: Path, isolated_appdata: Path, monkeypatch: pytest.MonkeyPatch, allow_memory: bool
) -> None:
    from language_review.memory.pipeline import MemorySession

    events = []

    class Session:
        def prepare(self, units: Any) -> None:
            events.append("prepare")
            for unit in units:
                unit.memory_context = [
                    {"id": "rule", "state": "rejected", "text": "Недоверенное свидетельство"}
                ]

        def finish(self, result: Any) -> None:
            events.append("finish")

    monkeypatch.setattr(MemorySession, "open", lambda *a, **kw: Session())
    provider = Provider()
    result = run(
        tmp_path,
        provider,
        llm_provider="chatgpt",
        llm_chatgpt_allow_cloud=True,
        llm_chatgpt_allow_memory=allow_memory,
        analysis_mode="deep",
        analysis_strategies=("grammar",),
    )
    assert events == ["prepare", "finish"]
    assert all(
        ("memory_context" in item) is allow_memory for _, p in provider.calls for item in p["items"]
    )
    analysis = result.engines[0].details["analysis"]
    assert analysis["memory_withheld"] == (0 if allow_memory else 2)
    assert len(analysis["memory_snapshot_sha256"]) == 64


@pytest.mark.parametrize(
    "model,effort,valid",
    [
        (m, e, e in allowed)
        for m, allowed in CHATGPT_EFFORTS.items()
        for e in ("none", "low", "medium", "high", "xhigh", "max", "minimal")
    ],
)
def test_model_effort_validation(
    tmp_path: Path, isolated_appdata: Path, model: str, effort: str, valid: bool
) -> None:
    config = replace(
        load_config(),
        check=CheckSettings(
            llm_provider="chatgpt", llm_chatgpt_model=model, llm_chatgpt_effort=effort
        ),
    )
    if valid:
        assert config.check_settings_for_profile(None)[0].llm_chatgpt_effort == effort
    else:
        with pytest.raises(ConfigurationError):
            config.check_settings_for_profile(None)


def test_default_local_uses_single_search_and_no_oauth(
    tmp_path: Path, isolated_appdata: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kw: Any) -> Any:
        pytest.fail("local baseline must not use OAuth")

    monkeypatch.setattr("language_review.chatgpt_auth.ChatGptAuth.__init__", forbidden)
    provider = Provider()
    result = run(tmp_path, provider)
    assert len(provider.calls) == 1 and result.status == "clean"
    assert check_document(result)["schema_version"] == "1.0"


def test_parallel_requests_respect_concurrency_and_global_budget(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    active = maximum = 0
    mutex = threading.Lock()
    rendezvous = threading.Barrier(2, timeout=10)

    def respond(system: str, payload: dict[str, Any]) -> Any:
        nonlocal active, maximum
        with mutex:
            active += 1
            maximum = max(maximum, active)
        try:
            rendezvous.wait()
        finally:
            with mutex:
                active -= 1
        return {"findings": []}

    provider = Provider(respond)
    result = run(tmp_path, provider, analysis_concurrency=2, llm_batch_items=1)
    assert maximum == 2 and result.status == "clean" and result.engines[0].requests == 2
    # Бюджет разрешает только один вызов, поэтому второй сценарий не ждёт пару.
    provider = Provider()
    result = run(
        tmp_path, provider, analysis_concurrency=2, llm_batch_items=1, analysis_max_requests=1
    )
    assert len(provider.calls) == 1 and result.engines[0].requests == 1
    assert result.status == "partial"


def test_evaluator_compares_new_reports_and_preserves_pass_telemetry(
    tmp_path: Path, isolated_appdata: Path
) -> None:
    from support.evaluation_helpers import add_label, make_corpus, write_source

    from language_review.evaluation.comparison import compare
    from language_review.evaluation.scoring import score

    corpus = make_corpus("Эта ошыбка.")
    add_label(corpus, "ошыбка", corrections=("ошибка",))
    inputs = tmp_path / "inputs"
    write_source(corpus, inputs)
    provider = Provider(
        lambda s, p: {
            "findings": [finding(item["id"]) for item in p["items"]]
            if "Дополнительный поисковый проход" in s
            else []
        }
    )
    config = replace(
        load_config(),
        check=CheckSettings(
            engines=("llm",), analysis_mode="deep", analysis_strategies=("grammar",)
        ),
    )
    baseline_config = replace(config, check=replace(config.check, analysis_mode="normal"))
    baseline = check_paths(
        [str(inputs)], baseline_config, engine_factory=lambda n, s: LlmEngine(s, provider)
    )
    baseline_score = score(corpus, review_document(baseline), {"root-0001": "demo"}, "dev")
    result = check_paths([str(inputs)], config, engine_factory=lambda n, s: LlmEngine(s, provider))
    evaluation = score(corpus, review_document(result), {"root-0001": "demo"}, "dev")
    assert evaluation["report_kind"] == "synthetic_contract_check"
    assert evaluation["summary"]["detection"]["tp"] == 1
    metadata = evaluation["manifest"]["engine_metadata"][0]
    assert metadata["statistics"]["requests"] == 2
    assert metadata["tokens"] == {"prompt": 20, "completion": 10}
    assert metadata["details"]["analysis"]["passes"][1]["pass"] == "grammar"
    comparison = compare(baseline_score, evaluation)
    assert len(comparison["recovered_fn"]) == len(comparison["new_tp"]) == 1
    assert comparison["new_fp"] == comparison["lost_tp"] == []


def test_background_memory_cannot_inherit_cloud_consent() -> None:
    from language_review.memory.store import MemoryFeatureError
    from language_review.memory.worker import process_one

    class Store:
        def claim(self, *args: Any, **kw: Any) -> None:
            pytest.fail("cloud worker must fail before touching the queue")

    with pytest.raises(MemoryFeatureError, match="memory_cloud_consolidation_not_supported"):
        process_one(
            Store(), None, CheckSettings(llm_provider="chatgpt", llm_chatgpt_allow_cloud=True)
        )
