from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4, uuid5

from language_review.memory.safety import digest
from language_review.memory.settings import MemorySettings, project_uuid
from language_review.memory.store import MemoryError, Store

if TYPE_CHECKING:
    from pathlib import Path

    from language_review.check_models import CheckResult, CheckUnit
    from language_review.extract_models import ExtractResult


class MemorySession:
    def __init__(self, store: Store, run_id: str, bindings: dict[str, str], dataset: str) -> None:
        self.store, self.run_id, self.bindings, self.dataset = store, run_id, bindings, dataset
        self.diagnostics: dict[str, Any] = {"queries": [], "context_units": 0}

    @classmethod
    def open(
        cls,
        settings: MemorySettings,
        extract: ExtractResult,
        options: dict[str, Any],
        *,
        policies: list[dict[str, Any]],
    ) -> MemorySession | None:
        if options.get("enabled") is False:
            return None
        if not (options.get("enabled") or settings.enabled):
            return None
        roots = {root["id"] for root in extract.roots}
        bindings = settings.projects
        if options.get("project"):
            if len(roots) != 1:
                raise MemoryError("memory_single_project_requires_one_root")
            bindings = {next(iter(roots)): project_uuid(options["project"])}
        if set(bindings) != roots or not roots:
            raise MemoryError("memory_explicit_project_bindings_required")
        if options.get("mode"):
            settings = replace(settings, recall_mode=options["mode"])
        store = Store(settings)
        store.health()
        try:
            run_id = str(UUID(options["run_id"])) if options.get("run_id") else str(uuid4())
        except (ValueError, TypeError, AttributeError) as exc:
            raise MemoryError("memory_run_id_invalid") from exc
        dataset = options.get("dataset") or (
            "synthetic" if settings.knowledge_dataset == "synthetic" else "operational"
        )
        store.begin(run_id, bindings, dataset, "live")
        session = cls(store, run_id, bindings, dataset)
        session.diagnostics["policy_sha256"] = {
            document.id: digest(policy)
            for document, policy in zip(extract.documents, policies, strict=True)
        }
        return session

    def prepare(self, units: list[CheckUnit]) -> None:
        from language_review.memory.search import recall

        # Только masked LLM view; секретный скаляр не отправляется embedding-сервису.
        cache: dict[str, dict[str, Any]] = {}
        for unit in units:
            if not unit.checked_by("llm"):
                continue
            project = self.bindings[unit.document.root]
            key = digest([project, unit.kind, unit.views["llm"].text])
            if key not in cache:
                cache[key] = recall(
                    self.store,
                    project,
                    unit.views["llm"].text,
                    kind=unit.kind,
                    dataset=(
                        "synthetic"
                        if self.store.settings.knowledge_dataset == "synthetic"
                        else self.dataset
                    ),
                    hybrid=self.store.settings.recall_mode == "hybrid",
                )
            recalled = cache[key]
            unit.memory_context = recalled["results"]
            self.diagnostics["queries"].append(
                {
                    "unit": unit.index,
                    "document_id": unit.document.id,
                    "mode": recalled["mode"],
                    "reason": recalled["reason"],
                    "rules": [row["id"] for row in recalled["results"]],
                }
            )
            self.diagnostics["context_units"] += bool(unit.memory_context)
        if not self.diagnostics["queries"]:
            self.diagnostics["recall_reason"] = "no_llm_units"

    def finish(self, result: CheckResult) -> None:
        from language_review.review_report import review_document

        result.memory = {
            **self.store.ingest(self.run_id, review_document(result), self.diagnostics),
            **self.diagnostics,
        }


def import_review(
    store: Store, path: Path, roots: dict[str, Path], bindings: dict[str, str], dataset: str
) -> dict[str, Any]:
    from language_review.review_verify import load_review, verify_review

    report = load_review(path)
    if verify_review(path, roots)["status"] != "current":
        raise MemoryError("memory_import_source_not_current")
    expected = {root["id"] for root in report["analysis"]["roots"]}
    if set(roots) != expected or set(bindings) != expected:
        raise MemoryError("memory_import_bindings_required")
    bindings = {root: project_uuid(value) for root, value in bindings.items()}
    # Повторный импорт тех же байтов/происхождения — та же доставка, не новый live-прогон.
    run_id = str(
        uuid5(UUID("ed097e40-18fe-44c8-b3d4-e14231f6a530"), digest([report, bindings, dataset]))
    )
    store.health()
    store.begin(run_id, bindings, dataset, "historical")
    return store.ingest(run_id, report, {"import": "source_verified", "diagnosis": "unverified"})
