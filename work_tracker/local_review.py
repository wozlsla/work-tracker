from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import PurePosixPath

from .models import CommitRecord, CommitReview, GitFileChange, SemanticChange


SOURCE = "codex-backfill"
MODEL = "Codex · 기존 이력 보강"

_SOURCE_SUFFIXES = {".h", ".hpp", ".cpp", ".c", ".cc", ".cs", ".py", ".js", ".ts", ".tsx", ".jsx"}
_ASSET_SUFFIXES = {".uasset", ".umap", ".wav", ".mp3", ".ogg", ".png", ".jpg", ".jpeg", ".fbx"}
_NETWORK_MARKERS = ("replication", "replicate", "onrep", "server", "client", "rpc", "multicast", "복제")


def backfill_existing_reviews(commits: list[CommitRecord], analyzed_at: str | None = None) -> int:
    """Fill the current baseline without calling an external API.

    The resulting review is tied to the immutable commit hash. Rescans restore it,
    while commits created later remain pending until the user runs OpenAI analysis.
    """
    timestamp = analyzed_at or datetime.now(UTC).isoformat()
    changed = 0
    for commit in commits:
        if commit.working_tree or (commit.review.status == "ready" and commit.review.source == "openai"):
            continue
        commit.review = build_local_review(commit, timestamp)
        changed += 1
    return changed


def build_local_review(commit: CommitRecord, analyzed_at: str | None = None) -> CommitReview:
    timestamp = analyzed_at or datetime.now(UTC).isoformat()
    files = commit.files
    semantic = [_clean_semantic(item) for item in commit.semantic_changes if item.component or item.changes]
    inferred = _infer_component_changes(files)
    components = _merge_components(semantic, inferred)[:12]
    domains = _unique(item.domain for item in files if item.domain)
    areas = _unique(item.area for item in files if item.area)
    symbols = _unique(symbol for item in semantic for symbol in item.symbols)[:20]
    change_type = _change_type(commit)
    impact = _impact(commit)
    confidence = "high" if semantic else ("medium" if files else "low")
    subject = commit.subject.strip() or "제목 없는 변경"
    component_names = [item.component for item in components if item.component]
    primary = ", ".join(component_names[:3])
    scope = _scope_text(commit, domains, primary)
    pr_summary = [subject]
    if scope:
        pr_summary.append(scope)
    if files:
        pr_summary.append(
            f"{len(files)}개 파일에서 +{commit.insertions}/-{commit.deletions} 변경을 확인했습니다. "
            f"세부 내역은 {_status_summary(files)}입니다."
        )
    elif commit.is_merge:
        pr_summary.append("병합 메타데이터를 기준으로 브랜치 통합 이력을 기록했습니다.")

    structure_flow = _structure_flow(commit, components, domains)
    network_flow = _network_flow(commit)
    highlights = _highlights(commit, components, domains)
    risks = _risks(commit, impact)
    checks = _checks(commit, network_flow)
    references = _references(commit)

    return CommitReview(
        status="ready",
        source=SOURCE,
        model=MODEL,
        analyzed_at=timestamp,
        commit_fingerprint=commit.hash,
        title=f"{commit.short_hash} 구현 분석",
        summary=f"{change_type} 중심의 변경입니다. {scope or subject}",
        change_type=change_type,
        impact=impact,
        confidence=confidence,
        highlights=highlights,
        risks=risks,
        checks=checks,
        domains=domains,
        areas=areas,
        symbols=symbols,
        evidence=_evidence(files),
        pr_summary=pr_summary,
        structure_flow=structure_flow,
        component_changes=components,
        network_flow=network_flow,
        references=references,
        generated_by="codex-backfill:v1",
    )


def _clean_semantic(item: SemanticChange) -> SemanticChange:
    return SemanticChange(
        component=item.component.strip(),
        changes=_unique(value.strip() for value in item.changes if value.strip())[:12],
        symbols=_unique(value.strip() for value in item.symbols if value.strip())[:12],
    )


def _merge_components(primary: list[SemanticChange], fallback: list[SemanticChange]) -> list[SemanticChange]:
    merged: dict[str, SemanticChange] = {}
    for item in [*primary, *fallback]:
        key = _component_key(item.component)
        if not key:
            continue
        current = merged.setdefault(key, SemanticChange(component=item.component))
        current.changes = _unique([*current.changes, *item.changes])[:12]
        current.symbols = _unique([*current.symbols, *item.symbols])[:12]
    return list(merged.values())


def _component_key(value: str) -> str:
    text = value.strip()
    if len(text) > 2 and text[0] in {"A", "U", "F", "I"} and text[1].isupper():
        text = text[1:]
    return text.casefold()


def _infer_component_changes(files: list[GitFileChange]) -> list[SemanticChange]:
    groups: dict[str, list[GitFileChange]] = defaultdict(list)
    for file in files:
        groups[_component_name(file)].append(file)
    ranked = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0].casefold()))[:10]
    result = []
    for component, changes in ranked:
        descriptions = []
        statuses = Counter(item.status.upper()[:1] for item in changes)
        kind = _file_kind(changes)
        if statuses.get("A"):
            descriptions.append(f"{kind} {statuses['A']}개 추가")
        if statuses.get("M"):
            descriptions.append(f"{kind} {statuses['M']}개 수정")
        if statuses.get("D"):
            descriptions.append(f"{kind} {statuses['D']}개 삭제")
        if not descriptions:
            descriptions.append(f"{kind} {len(changes)}개 변경")
        lines = sum(item.insertions + item.deletions for item in changes)
        if lines:
            descriptions.append(f"텍스트 변경 +{sum(item.insertions for item in changes)}/-{sum(item.deletions for item in changes)}")
        result.append(SemanticChange(component=component, changes=descriptions))
    return result


def _component_name(file: GitFileChange) -> str:
    path = PurePosixPath(file.path.replace("\\", "/"))
    stem = path.stem or path.name or "Project"
    if stem.casefold() in {"defaultengine", "defaultgame", "defaultinput", "defaulteditor"}:
        return "Project Settings"
    if stem.casefold().endswith(".build"):
        return stem[:-6] + " Module"
    return stem


def _file_kind(files: list[GitFileChange]) -> str:
    suffixes = {PurePosixPath(item.path).suffix.casefold() for item in files}
    if suffixes & _SOURCE_SUFFIXES:
        return "소스 파일"
    if suffixes & _ASSET_SUFFIXES or any(item.binary for item in files):
        return "에셋"
    if suffixes & {".ini", ".json", ".uproject", ".uplugin", ".cs"}:
        return "설정 파일"
    return "파일"


def _change_type(commit: CommitRecord) -> str:
    text = f"{commit.subject} {commit.body}".casefold()
    if commit.is_merge or text.startswith("merge"):
        return "브랜치 병합"
    if any(token in text for token in ("fix", "bug", "수정", "오류", "버그")):
        return "버그 수정"
    if any(token in text for token in ("refactor", "리팩터", "정리", "cleanup")):
        return "리팩터링"
    if any(token in text for token in ("add", "feat", "기능", "추가", "구현")):
        return "기능 추가"
    suffixes = {PurePosixPath(item.path).suffix.casefold() for item in commit.files}
    if suffixes and suffixes <= _ASSET_SUFFIXES:
        return "에셋 변경"
    if suffixes and suffixes <= {".ini", ".json", ".uproject", ".uplugin"}:
        return "설정 변경"
    return "구현 변경"


def _impact(commit: CommitRecord) -> str:
    paths = [item.path.casefold() for item in commit.files]
    text = " ".join([commit.subject, commit.body, *commit.semantic_flow, *[value for item in commit.semantic_changes for value in item.changes]]).casefold()
    total_lines = commit.insertions + commit.deletions
    public_contract = any("/public/" in f"/{path}" or path.endswith((".build.cs", ".uproject", ".uplugin")) for path in paths)
    network = any(marker in text for marker in _NETWORK_MARKERS)
    if len(commit.files) >= 40 or total_lines >= 1_500 or (network and public_contract):
        return "high"
    if len(commit.files) >= 6 or total_lines >= 300 or public_contract or network:
        return "medium"
    return "low"


def _scope_text(commit: CommitRecord, domains: list[str], primary: str) -> str:
    if primary:
        return f"{primary}를 중심으로 선언·구현 및 연관 리소스를 함께 변경했습니다."
    if domains:
        return f"{', '.join(domains[:3])} 영역의 파일과 리소스를 변경했습니다."
    if commit.is_merge:
        branch = commit.branches[0] if commit.branches else "관련"
        return f"{branch} 브랜치의 변경을 통합했습니다."
    return ""


def _status_summary(files: list[GitFileChange]) -> str:
    names = {"A": "추가", "M": "수정", "D": "삭제", "R": "이동"}
    counts = Counter(item.status.upper()[:1] for item in files)
    parts = [f"{names[key]} {counts[key]}개" for key in ("A", "M", "D", "R") if counts[key]]
    binary = sum(1 for item in files if item.binary)
    if binary:
        parts.append(f"바이너리 {binary}개")
    return ", ".join(parts) or f"변경 {len(files)}개"


def _structure_flow(commit: CommitRecord, components: list[SemanticChange], domains: list[str]) -> list[str]:
    if commit.semantic_flow:
        return _unique(commit.semantic_flow)[:14]
    component_names = [item.component for item in components if item.component][:5]
    if len(component_names) >= 2:
        return component_names
    if domains and component_names:
        return [domains[0], component_names[0], "연관 구현 및 리소스"]
    if commit.is_merge:
        return ["Source branch", "Merge commit", "Target branch"]
    return component_names


def _network_flow(commit: CommitRecord) -> list[str]:
    candidates = [*commit.semantic_flow]
    for item in commit.semantic_changes:
        candidates.extend(item.changes)
        candidates.extend(item.symbols)
    relevant = [value for value in candidates if any(marker in value.casefold() for marker in _NETWORK_MARKERS)]
    return _unique(relevant)[:14]


def _highlights(commit: CommitRecord, components: list[SemanticChange], domains: list[str]) -> list[str]:
    result = []
    for item in components[:4]:
        if item.changes:
            result.append(f"{item.component}: {item.changes[0]}")
    if not result and commit.is_merge:
        result.append("브랜치 병합 지점을 활동 이력에 연결했습니다.")
    if domains:
        result.append(f"영향 영역: {', '.join(domains[:4])}")
    return _unique(result)[:6]


def _risks(commit: CommitRecord, impact: str) -> list[str]:
    result = []
    files = commit.files
    if any(item.binary for item in files):
        result.append("바이너리 에셋은 텍스트 diff만으로 내부 변경을 검증할 수 없습니다.")
    if any("/public/" in f"/{item.path.casefold()}" or item.path.casefold().endswith((".h", ".hpp")) for item in files):
        result.append("공개 헤더 변경이 Blueprint 또는 다른 모듈의 호출부에 영향을 줄 수 있습니다.")
    if any(PurePosixPath(item.path).suffix.casefold() in {".ini", ".uproject", ".uplugin", ".cs"} for item in files):
        result.append("프로젝트·빌드 설정 변경은 실행 환경과 패키징 결과에 영향을 줄 수 있습니다.")
    if commit.is_merge:
        result.append("자동 병합 성공만으로 양쪽 브랜치의 동작 충돌이 없다고 단정할 수 없습니다.")
    if impact == "high":
        result.append("변경 범위가 커서 기능 단위 회귀 원인을 분리하기 어려울 수 있습니다.")
    if not result:
        result.append("변경된 기능의 호출 경로와 기존 사용처에서 회귀가 없는지 확인해야 합니다.")
    return _unique(result)[:6]


def _checks(commit: CommitRecord, network_flow: list[str]) -> list[str]:
    suffixes = {PurePosixPath(item.path).suffix.casefold() for item in commit.files}
    paths = [item.path.casefold() for item in commit.files]
    result = []
    if suffixes & _SOURCE_SUFFIXES:
        result.append("대상 모듈과 프로젝트 전체를 컴파일하고 변경된 호출부를 실행합니다.")
    if any(item.binary or PurePosixPath(item.path).suffix.casefold() in _ASSET_SUFFIXES for item in commit.files):
        result.append("Unreal Editor에서 에셋 참조, 로딩, 저장 및 대표 플레이 흐름을 확인합니다.")
    if network_flow:
        result.append("서버 권한, 클라이언트 복제, OnRep/RPC 흐름을 멀티플레이 환경에서 검증합니다.")
    if any("ui" in path or "widget" in path or "hud" in path for path in paths):
        result.append("UI 생성·갱신·입력 흐름과 해상도별 표시를 확인합니다.")
    if any(PurePosixPath(path).suffix.casefold() in {".ini", ".uproject", ".uplugin"} for path in paths):
        result.append("Editor 재시작 후 프로젝트 설정과 패키징 구성이 유지되는지 확인합니다.")
    if commit.is_merge:
        result.append("병합된 양쪽 브랜치의 핵심 시나리오를 통합 상태에서 다시 실행합니다.")
    if not result:
        result.append("커밋 제목의 사용자 시나리오를 재현하고 이전 동작과 비교합니다.")
    return _unique(result)[:6]


def _evidence(files: list[GitFileChange]) -> list[str]:
    ranked = sorted(files, key=lambda item: (-(item.insertions + item.deletions), item.path.casefold()))
    result = []
    for item in ranked[:16]:
        stats = "binary" if item.binary else f"+{item.insertions}/-{item.deletions}"
        result.append(f"{item.status} · {item.path} · {stats}")
    return result


def _references(commit: CommitRecord) -> list[str]:
    text = f"{commit.subject}\n{commit.body}"
    refs = re.findall(r"(?i)\b(?:refs?|fixe[sd]?|close[sd]?)\s+#(\d+)|(?<!\w)#(\d+)", text)
    numbers = _unique(next((value for value in match if value), "") for match in refs)
    return [f"#{value}" for value in numbers if value][:10]


def _unique(values) -> list[str]:
    result = []
    seen = set()
    for value in values:
        text = str(value).strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result
