from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .models import (
    ChangeRecord,
    CommitRecord,
    CommitReview,
    ProjectContext,
    ProjectReport,
    RiskRecord,
    SemanticChange,
)
from .scanner import ScanInventory, explain_file


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def build_report(
    project_name: str,
    project_root: Path,
    output_dir: Path,
    since: str,
    until: str,
    inventory: ScanInventory,
    commits: list[CommitRecord],
    context: ProjectContext,
    previous_state: dict[str, Any] | None,
) -> ProjectReport:
    created = datetime.now(UTC)
    restore_manual_reviews(commits, previous_state)
    changes = compare_files(previous_state, inventory)
    risks = analyze_risks(project_name, inventory, commits, changes, context)
    return ProjectReport(
        schema_version="2.2",
        run_id=created.strftime("%Y%m%d-%H%M%S-%f"),
        created_at=created.isoformat(),
        project_name=project_name,
        project_path=str(project_root.resolve()),
        output_path=str(output_dir.resolve()),
        period_since=since,
        period_until=until,
        milestone=context.milestone,
        goals=context.goals,
        files=inventory.files,
        modules=inventory.modules,
        classes=inventory.classes,
        relationships=inventory.relationships,
        domains=inventory.domains,
        commits=commits,
        changes=changes,
        risks=risks,
        warnings=inventory.warnings,
        skipped=inventory.skipped,
    )


def restore_manual_reviews(commits: list[CommitRecord], previous_state: dict[str, Any] | None) -> None:
    """Keep immutable-hash OpenAI and baseline reviews across rescans.

    A review belongs to an immutable commit hash. An amended commit receives a new
    hash and therefore deliberately returns to the pending state.
    """
    previous_commits = {
        str(item.get("hash", "")): item
        for item in (previous_state or {}).get("commits", [])
        if isinstance(item, dict) and item.get("hash")
    }
    for commit in commits:
        status = "unavailable" if commit.working_tree else "pending"
        commit.review = CommitReview(status=status, commit_fingerprint=commit.hash)
        old_commit = previous_commits.get(commit.hash)
        old_review = old_commit.get("review") if isinstance(old_commit, dict) else None
        if not isinstance(old_review, dict):
            continue
        if old_review.get("source") not in {"openai", "codex-backfill"} or old_review.get("status") != "ready":
            continue
        if old_review.get("commit_fingerprint") != commit.hash:
            continue
        commit.review = _commit_review_from_dict(old_review)


def _commit_review_from_dict(payload: dict[str, Any]) -> CommitReview:
    scalar_fields = (
        "status", "source", "model", "analyzed_at", "commit_fingerprint", "error",
        "title", "summary", "change_type", "impact", "confidence", "generated_by",
    )
    list_fields = (
        "highlights", "risks", "checks", "domains", "areas", "symbols", "evidence",
        "pr_summary", "structure_flow", "network_flow", "references",
    )
    values: dict[str, Any] = {}
    for name in scalar_fields:
        value = payload.get(name)
        if isinstance(value, str):
            values[name] = value
    for name in list_fields:
        value = payload.get(name)
        if isinstance(value, list):
            values[name] = [str(item) for item in value if isinstance(item, (str, int, float))]
    components = []
    for item in payload.get("component_changes", []):
        if not isinstance(item, dict):
            continue
        components.append(SemanticChange(
            component=str(item.get("component", "")),
            changes=[str(value) for value in item.get("changes", []) if isinstance(value, (str, int, float))],
            symbols=[str(value) for value in item.get("symbols", []) if isinstance(value, (str, int, float))],
        ))
    values["component_changes"] = components
    return CommitReview(**values)


def load_previous_state(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists() or path.stat().st_size > 128 * 1024 * 1024:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def compare_files(previous: dict[str, Any] | None, inventory: ScanInventory) -> list[ChangeRecord]:
    if not previous:
        return []
    old_files = {
        str(item.get("path", "")): item
        for item in previous.get("files", [])
        if isinstance(item, dict) and item.get("path")
    }
    current = {item.path: item for item in inventory.files}
    changes: list[ChangeRecord] = []
    for path, file in current.items():
        old = old_files.get(path)
        if old is None:
            status = "added"
        elif old.get("fingerprint") != file.fingerprint:
            status = "modified"
        else:
            continue
        changes.append(ChangeRecord(path, status, file.kind, file.area, file.domain, file.module, explain_file(file)))
    for path, old in old_files.items():
        if path in current:
            continue
        changes.append(ChangeRecord(
            path=path,
            status="deleted",
            kind=str(old.get("kind", "Other")),
            area=str(old.get("area", "Unknown")),
            domain=str(old.get("domain", "core")),
            module=str(old.get("module", "")),
            meaning="이전 스냅샷에는 있었지만 현재 프로젝트에서 찾을 수 없습니다.",
        ))
    order = {"added": 0, "modified": 1, "deleted": 2}
    return sorted(changes, key=lambda item: (order.get(item.status, 9), item.path.casefold()))


def analyze_risks(
    project_name: str,
    inventory: ScanInventory,
    commits: list[CommitRecord],
    changes: list[ChangeRecord],
    context: ProjectContext,
) -> list[RiskRecord]:
    risks: list[RiskRecord] = []
    active_files = [file for commit in commits for file in commit.files]
    working = next((commit for commit in commits if commit.working_tree), None)

    ownership_hits: dict[tuple[str, str], list[str]] = defaultdict(list)
    relevant_rules = [rule for rule in context.ownership if not rule.repository or rule.repository.casefold() == project_name.casefold()]
    for commit in commits:
        if commit.working_tree:
            continue
        author_tokens = {commit.member.casefold(), commit.author.casefold(), commit.email.casefold()}
        for file in commit.files:
            for rule in relevant_rules:
                if not any(_path_matches(file.path, prefix) for prefix in rule.paths):
                    continue
                owners = {rule.owner.casefold(), *(item.casefold() for item in rule.aliases)}
                if not any(any(owner in token for token in author_tokens) for owner in owners if owner):
                    ownership_hits[(rule.label, rule.owner)].append(f"{commit.short_hash} · {file.path}")
    for (label, owner), evidence in ownership_hits.items():
        risks.append(RiskRecord(
            id=f"ownership-{_slug(label)}-{_slug(owner)}",
            severity="high",
            title=f"담당 경계 확인 필요 · {label}",
            detail=f"{owner} 담당 경로에 다른 작성자의 변경이 감지되었습니다. 정상 협업인지 API/에셋 충돌 가능성이 있는지 확인하세요.",
            evidence=_dedupe(evidence)[:10],
            action=f"{owner}와 변경 의도, 공개 인터페이스 영향, 병합 순서를 확인합니다.",
        ))

    binary = [file for file in active_files if file.binary]
    if binary:
        paths = _dedupe([file.path for file in binary])
        risks.append(RiskRecord(
            id="binary-assets",
            severity="high" if len(paths) >= 8 else "medium",
            title="병합 불가능한 바이너리 에셋 변경",
            detail=f"텍스트 diff로 검증하기 어려운 바이너리 파일 {len(paths)}개가 활동 기간에 변경되었습니다.",
            evidence=paths[:12],
            action="에셋 소유자, 기준 브랜치, Unreal Editor 검증 여부를 기록하고 동시에 편집하지 않습니다.",
        ))

    public_surface = [
        file.path for file in active_files
        if file.path.casefold().endswith((".h", ".hpp", ".build.cs", ".uproject", ".uplugin"))
        and ("/public/" in f"/{file.path.casefold()}" or file.path.casefold().endswith((".build.cs", ".uproject", ".uplugin")))
    ]
    if public_surface:
        risks.append(RiskRecord(
            id="public-contract",
            severity="medium",
            title="공개 계약 또는 모듈 경계 변경",
            detail="공개 헤더·빌드 규칙·디스크립터 변경은 다른 모듈과 Blueprint 호출부에 연쇄 영향을 줄 수 있습니다.",
            evidence=_dedupe(public_surface)[:12],
            action="전체 빌드, Blueprint 참조, 모듈 의존성, API 사용처를 함께 검증합니다.",
        ))

    large_commits = [
        commit for commit in commits
        if not commit.working_tree and (len(commit.files) >= 40 or commit.insertions + commit.deletions >= 1_500)
    ]
    if large_commits:
        risks.append(RiskRecord(
            id="large-change",
            severity="medium",
            title="검토 범위가 큰 변경",
            detail="한 번에 검토하기 어려운 대형 커밋이 있습니다. 기능·리팩터링·에셋 변경이 섞이면 회귀 원인을 찾기 어렵습니다.",
            evidence=[f"{item.short_hash} · {len(item.files)} files · +{item.insertions}/-{item.deletions} · {item.subject}" for item in large_commits[:8]],
            action="도메인별로 검토자를 지정하고 테스트 근거를 커밋 또는 PR 단위로 연결합니다.",
        ))

    merge_commits = [item for item in commits if item.is_merge]
    if merge_commits:
        risks.append(RiskRecord(
            id="merge-audit",
            severity="low",
            title="병합 커밋 사후 확인",
            detail=f"활동 기간에 병합 커밋 {len(merge_commits)}개가 있습니다. 자동 병합 성공은 동작 충돌 부재를 보장하지 않습니다.",
            evidence=[f"{item.short_hash} · {item.subject}" for item in merge_commits[:8]],
            action="양쪽 브랜치의 핵심 시나리오와 설정·에셋 참조를 통합 상태에서 재검증합니다.",
        ))

    cycles = _module_cycles(inventory)
    if cycles:
        risks.append(RiskRecord(
            id="module-cycle",
            severity="high",
            title="모듈 의존성 순환",
            detail="모듈 간 순환 의존성은 빌드 순서와 경계 설계를 불안정하게 만듭니다.",
            evidence=[" → ".join(cycle) for cycle in cycles[:8]],
            action="공통 계약을 더 낮은 계층 모듈로 옮기고 양방향 의존을 이벤트 또는 인터페이스로 분리합니다.",
        ))

    god_classes = [item for item in inventory.classes if len(item.properties) + len(item.functions) + len(item.components) >= 70]
    if god_classes:
        risks.append(RiskRecord(
            id="oversized-class",
            severity="medium",
            title="책임이 과도하게 집중된 클래스",
            detail="리플렉션 멤버와 컴포넌트가 많은 클래스는 변경 파급 범위와 Blueprint 결합도가 커질 가능성이 높습니다.",
            evidence=[f"{item.name} · {len(item.properties)} properties · {len(item.functions)} functions · {item.path}" for item in god_classes[:10]],
            action="상태, 입력, 전투, UI 책임을 컴포넌트 또는 서브시스템으로 분리할 수 있는지 검토합니다.",
        ))

    degree = Counter()
    for edge in inventory.relationships:
        if edge.kind not in {"includes"}:
            degree[edge.source] += 1
            degree[edge.target] += 1
    coupled = [(name, count) for name, count in degree.most_common(10) if count >= 30]
    if coupled:
        risks.append(RiskRecord(
            id="coupling-hotspot",
            severity="medium",
            title="구조 결합도 핫스팟",
            detail="다수의 상속·컴포넌트·입력·인터페이스 관계가 한 노드에 집중되어 있습니다.",
            evidence=[f"{name} · {count} relationships" for name, count in coupled],
            action="해당 노드 변경 시 영향 도메인을 먼저 확인하고 계약 테스트 또는 컴파일 검증을 추가합니다.",
        ))

    if working and len(working.files) >= 25:
        risks.append(RiskRecord(
            id="working-tree-size",
            severity="medium",
            title="커밋되지 않은 변경 범위가 큼",
            detail=f"현재 작업 트리에 {len(working.files)}개 파일 변경이 남아 있습니다.",
            evidence=[item.path for item in working.files[:12]],
            action="기능 단위로 커밋을 나누고 에셋·설정·코드 변경을 함께 검증한 뒤 병합합니다.",
        ))

    if inventory.skipped.get("sensitive", 0):
        risks.append(RiskRecord(
            id="sensitive-files",
            severity="info",
            title="민감 파일은 수집에서 제외됨",
            detail=f"자격 증명 가능성이 있는 파일 {inventory.skipped['sensitive']}개는 이름·내용을 보고서에 넣지 않았습니다.",
            action="저장소 추적 여부와 .gitignore 정책을 별도로 점검합니다.",
        ))

    if not risks:
        risks.append(RiskRecord(
            id="no-signal",
            severity="info",
            title="자동 탐지된 고위험 신호 없음",
            detail="현재 수집 근거에서는 명확한 구조·병합 위험이 발견되지 않았습니다. 이는 테스트 통과를 의미하지는 않습니다.",
            action="빌드, 핵심 플레이 흐름, 에셋 참조를 프로젝트 기준에 따라 검증합니다.",
        ))
    return sorted(risks, key=lambda item: (SEVERITY_ORDER.get(item.severity, 9), item.title.casefold()))


def activity_by_member(commits: list[CommitRecord]) -> list[dict[str, Any]]:
    groups: dict[str, list[CommitRecord]] = defaultdict(list)
    for commit in commits:
        groups[commit.member].append(commit)
    result = []
    for member, items in groups.items():
        files = {file.path for item in items for file in item.files}
        domains = Counter(file.domain for item in items for file in item.files)
        result.append({
            "member": member,
            "role": next((item.role for item in items if item.role), "Unknown"),
            "commits": sum(1 for item in items if not item.working_tree),
            "files": len(files),
            "insertions": sum(item.insertions for item in items),
            "deletions": sum(item.deletions for item in items),
            "domains": [name for name, _ in domains.most_common(4)],
        })
    return sorted(result, key=lambda item: (-item["commits"], -item["files"], item["member"].casefold()))


def _module_cycles(inventory: ScanInventory) -> list[list[str]]:
    graph = {item.name: [dep for dep in item.dependencies if dep != item.name] for item in inventory.modules}
    names = {name.casefold(): name for name in graph}
    normalized = {name: [names[dep.casefold()] for dep in deps if dep.casefold() in names] for name, deps in graph.items()}
    cycles: set[tuple[str, ...]] = set()

    def visit(node: str, path: list[str], active: set[str]) -> None:
        if len(path) > 12:
            return
        for target in normalized.get(node, []):
            if target in active:
                start = path.index(target)
                cycle = path[start:] + [target]
                core = cycle[:-1]
                rotations = [tuple(core[index:] + core[:index]) for index in range(len(core))]
                cycles.add(min(rotations))
            else:
                visit(target, [*path, target], {*active, target})

    for name in normalized:
        visit(name, [name], {name})
    return [list(item) + [item[0]] for item in sorted(cycles)][:20]


def _path_matches(path: str, prefix: str) -> bool:
    clean_path = PurePosixPath(path.replace("\\", "/")).as_posix().casefold().lstrip("./")
    clean_prefix = PurePosixPath(prefix.replace("\\", "/")).as_posix().casefold().lstrip("./")
    return clean_path == clean_prefix.rstrip("/") or clean_path.startswith(clean_prefix.rstrip("/") + "/")


def _slug(value: str) -> str:
    return "-".join("".join(ch if ch.isalnum() else " " for ch in value.casefold()).split()) or "item"


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
