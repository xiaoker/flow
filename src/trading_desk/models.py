from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


Severity = Literal["critical", "high", "medium", "low", "info"]
Status = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    publisher: str
    dataset: str
    url: str
    source_grade: Literal["A", "B", "C"] = "A"
    date_semantics: str = "source_published"


@dataclass
class FetchArtifact:
    source: SourceSpec
    requested_date: str | None
    observed_at: str
    # Legacy composite archives may not have recorded transport status.
    # None means unknown, never an inferred successful HTTP response.
    status_code: int | None
    content_type: str
    raw_path: str
    metadata_path: str
    sha256: str
    row_count: int = 0
    normalized_path: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source"] = asdict(self.source)
        return payload


@dataclass(frozen=True)
class QualityFinding:
    check_id: str
    dataset: str
    status: Status
    severity: Severity
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

