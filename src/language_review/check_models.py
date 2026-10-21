from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from language_review.extract_models import ExtractDocument, ResolvedFragment
from language_review.masking import EngineView

if TYPE_CHECKING:
    from language_review.extract_models import ExtractResult


@dataclass
class CheckUnit:
    """Единица проверки: один фрагмент или соседние строки одного абзаца/комментария."""

    index: int
    document_index: int
    document: ExtractDocument
    fragments: tuple[ResolvedFragment, ...]
    fragment_ids: tuple[str, ...]
    kind: str
    context: str
    sentence_start: bool
    views: dict[str, EngineView]
    engines: tuple[str, ...]
    terms: dict[str, str]
    # Есть ли в виде для движка кириллица; без неё русскоязычные правила неприменимы.
    has_russian: bool = True
    # Применимость по движкам: движок → причина пропуска. Движок без записи проверяет единицу.
    skip_reasons: dict[str, str] = field(default_factory=dict)
    memory_context: list[dict[str, Any]] = field(default_factory=list)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    routing_reasons: list[str] = field(default_factory=list)

    def checked_by(self, engine: str) -> bool:
        return engine in self.engines and engine not in self.skip_reasons


@dataclass(frozen=True)
class RawFinding:
    """Замечание движка в координатах его вида (кодовые точки Unicode)."""

    engine: str
    unit: int
    view: str
    start: int
    end: int
    category: str
    rule_id: str | None
    rule_category: str | None
    message: str
    replacements: tuple[str, ...]
    replacement_origin: str
    confidence: str | None = None
    severity: str | None = None
    engine_excerpt: str | None = None


@dataclass(frozen=True)
class RejectedOutput:
    """Отбракованный результат движка с машинной причиной."""

    engine: str
    unit: int
    reason: str
    rule_id: str | None = None
    message: str | None = None
    excerpt: str | None = None
    raw: RawFinding | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class UnitOutcome:
    unit: int
    status: str
    reason: str | None = None
    findings: list[RawFinding] = field(default_factory=list)
    rejected: list[RejectedOutput] = field(default_factory=list)


@dataclass
class ProbeResult:
    status: str
    details: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    message: str | None = None


@dataclass
class EngineRun:
    name: str
    details: dict[str, Any]
    status: str = "ok"
    diagnostics: list[str] = field(default_factory=list)
    message: str | None = None
    units_total: int = 0
    units_checked: int = 0
    units_failed: int = 0
    units_skipped: int = 0
    fragments_checked: int = 0
    findings: int = 0
    rejected: int = 0
    filtered: int = 0
    requests: int = 0
    request_failures: int = 0
    elapsed_ms: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)

    def add_tokens(self, name: str, value: int | None) -> None:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            self.tokens[name] = self.tokens.get(name, 0) + value


class Engine(Protocol):
    name: str

    def describe(self) -> dict[str, Any]: ...

    def probe(self) -> ProbeResult: ...

    def run(self, units: Sequence[CheckUnit], run: EngineRun) -> Iterator[UnitOutcome]: ...

    def close(self) -> None: ...


@dataclass
class Suggestion:
    engines: list[str]
    origin: str
    status: str
    reasons: list[str]
    engine_replacement: str
    edit: tuple[int, int, str] | None


@dataclass
class Candidate:
    """Замечание после проверки диапазона и отображения на исходник."""

    raw: RawFinding
    unit: CheckUnit
    source_start: int
    source_end: int
    suggestions: list[Suggestion]


@dataclass
class Finding:
    id: str
    document_index: int
    category: str
    source_start: int
    source_end: int
    candidates: list[Candidate]
    suggestions: list[Suggestion]


@dataclass
class CheckDocument:
    extract: ExtractDocument
    status: str
    engines: tuple[str, ...]
    # Эффективные настройки документа (профиль и CLI) в публичном виде для отчёта.
    configuration: dict[str, Any] = field(default_factory=dict)
    units: list[CheckUnit] = field(default_factory=list)
    unit_status: dict[tuple[int, str], tuple[str, str | None]] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    rejected: list[RejectedOutput] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    filtered: list[RawFinding] = field(default_factory=list)


@dataclass
class CheckResult:
    extract: ExtractResult
    documents: list[CheckDocument]
    engines: list[EngineRun]
    status: str
    settings: dict[str, Any]
    memory: dict[str, Any] = field(default_factory=dict)
