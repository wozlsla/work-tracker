from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class RepositoryConfig:
    name: str
    path: str


@dataclass(slots=True)
class TeamMember:
    name: str
    role: str = "Unknown"
    aliases: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AreaRule:
    name: str
    aliases: list[str] = field(default_factory=list)
    members: list[str] = field(default_factory=list)
    match: list[str] = field(default_factory=list)


@dataclass(slots=True)
class OwnershipRule:
    repository: str
    owner: str
    label: str
    aliases: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProjectContext:
    members: list[TeamMember] = field(default_factory=list)
    areas: list[AreaRule] = field(default_factory=list)
    ownership: list[OwnershipRule] = field(default_factory=list)
    project_plan: str = ""
    milestone: str = ""
    goals: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class FileRecord:
    path: str
    extension: str
    kind: str
    area: str
    domain: str
    size: int
    modified_utc: str
    fingerprint: str
    module: str = ""
    generated: bool = False


@dataclass(slots=True)
class CodeMember:
    name: str
    type: str = ""
    category: str = ""
    detail: str = ""


@dataclass(slots=True)
class CodeClass:
    name: str
    path: str
    domain: str
    kind: str
    base_class: str = ""
    interfaces: list[str] = field(default_factory=list)
    components: list[CodeMember] = field(default_factory=list)
    properties: list[CodeMember] = field(default_factory=list)
    functions: list[CodeMember] = field(default_factory=list)
    summary: str = ""


@dataclass(slots=True)
class Relationship:
    source: str
    target: str
    kind: str
    path: str = ""
    detail: str = ""


@dataclass(slots=True)
class ModuleRecord:
    name: str
    path: str
    kind: str = "Game"
    dependencies: list[str] = field(default_factory=list)


@dataclass(slots=True)
class GitFileChange:
    path: str
    status: str = "M"
    insertions: int = 0
    deletions: int = 0
    binary: bool = False
    area: str = ""
    domain: str = ""


@dataclass(slots=True)
class SemanticChange:
    component: str = ""
    changes: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CommitReview:
    status: str = "pending"
    source: str = ""
    model: str = ""
    analyzed_at: str = ""
    commit_fingerprint: str = ""
    error: str = ""
    title: str = ""
    summary: str = ""
    change_type: str = "변경"
    impact: str = "low"
    confidence: str = "medium"
    highlights: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    areas: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    pr_summary: list[str] = field(default_factory=list)
    structure_flow: list[str] = field(default_factory=list)
    component_changes: list[SemanticChange] = field(default_factory=list)
    network_flow: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    generated_by: str = ""


@dataclass(slots=True)
class CommitRecord:
    hash: str
    short_hash: str
    author: str
    email: str
    date: str
    subject: str
    body: str = ""
    parents: list[str] = field(default_factory=list)
    files: list[GitFileChange] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    member: str = "Unknown"
    role: str = "Unknown"
    working_tree: bool = False
    semantic_changes: list[SemanticChange] = field(default_factory=list)
    semantic_flow: list[str] = field(default_factory=list)
    review: CommitReview = field(default_factory=CommitReview)

    @property
    def insertions(self) -> int:
        return sum(item.insertions for item in self.files)

    @property
    def deletions(self) -> int:
        return sum(item.deletions for item in self.files)

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


@dataclass(slots=True)
class ChangeRecord:
    path: str
    status: str
    kind: str
    area: str
    domain: str
    module: str = ""
    meaning: str = ""


@dataclass(slots=True)
class RiskRecord:
    id: str
    severity: str
    title: str
    detail: str
    evidence: list[str] = field(default_factory=list)
    action: str = ""


@dataclass(slots=True)
class DomainRecord:
    key: str
    title: str
    summary: str
    classes: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    interfaces: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ScanLimits:
    max_files: int = 100_000
    max_hash_bytes: int = 16 * 1024 * 1024
    max_text_bytes: int = 2 * 1024 * 1024
    max_symbols: int = 5_000
    max_relationships: int = 10_000
    git_timeout_seconds: int = 20
    git_output_bytes: int = 32 * 1024 * 1024


@dataclass(slots=True)
class ProjectReport:
    schema_version: str
    run_id: str
    created_at: str
    project_name: str
    project_path: str
    output_path: str
    period_since: str
    period_until: str
    milestone: str = ""
    goals: list[dict[str, str]] = field(default_factory=list)
    files: list[FileRecord] = field(default_factory=list)
    modules: list[ModuleRecord] = field(default_factory=list)
    classes: list[CodeClass] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    domains: list[DomainRecord] = field(default_factory=list)
    commits: list[CommitRecord] = field(default_factory=list)
    changes: list[ChangeRecord] = field(default_factory=list)
    risks: list[RiskRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["summary"] = self.summary
        return value

    @property
    def summary(self) -> dict[str, int]:
        return {
            "files": len(self.files),
            "modules": len(self.modules),
            "classes": len(self.classes),
            "relationships": len(self.relationships),
            "commits": sum(1 for item in self.commits if not item.working_tree),
            "changes": len(self.changes),
            "risks": len(self.risks),
            "critical_risks": sum(1 for item in self.risks if item.severity == "critical"),
        }
