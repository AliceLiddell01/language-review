from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from language_review.coordinates import SourceMap

if TYPE_CHECKING:
    from language_review.scanner import ScanResult


@dataclass(frozen=True)
class ProtectedSpan:
    start_char: int
    end_char: int
    reason: str


@dataclass(frozen=True)
class FragmentDraft:
    kind: str
    context: str
    text: str
    start_char: int
    end_char: int
    protected: tuple[ProtectedSpan, ...] = ()
    status: str = "extracted"
    reason: str | None = None


@dataclass
class ExtractDocument:
    id: str
    root: str
    path: str
    language: str | None
    format: str | None
    profile: str | None
    status: str
    reason: str | None = None
    source_sha256: str | None = None
    encoding: str | None = None
    bom: str | None = None
    fragments: list[FragmentDraft] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    source_map: SourceMap | None = field(default=None, repr=False)


@dataclass
class ExtractResult:
    roots: list[dict[str, Any]]
    documents: list[ExtractDocument]
    scan_result: ScanResult

    @property
    def error_count(self) -> int:
        return sum(document.status in {"error", "partial"} for document in self.documents)

    @property
    def skipped_count(self) -> int:
        return sum(document.status == "skipped" for document in self.documents)

    @property
    def unsupported_count(self) -> int:
        return sum(document.status == "unsupported" for document in self.documents)

    @property
    def extracted_count(self) -> int:
        return sum(document.status == "extracted" for document in self.documents)
