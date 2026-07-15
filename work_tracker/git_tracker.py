from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import CommitRecord, GitFileChange, ScanLimits, TeamMember
from .scanner import classify_area, classify_domain
from .semantic_diff import analyze_patch


class GitError(RuntimeError):
    pass


def resolve_period(since: str | None, until: str | None, days: int) -> tuple[str, str]:
    now = datetime.now(UTC)
    if until:
        end = _parse_date(until, end_of_day=True)
    else:
        end = now
    if since:
        start = _parse_date(since, end_of_day=False)
    else:
        start = end - timedelta(days=max(days, 1))
    if start > end:
        raise ValueError("--since는 --until보다 늦을 수 없습니다.")
    return start.isoformat(), end.isoformat()


def collect_git_activity(
    root: Path,
    since: str,
    until: str,
    members: list[TeamMember],
    area_rules,
    limits: ScanLimits,
    include_working_tree: bool,
    max_commits: int = 120,
) -> tuple[list[CommitRecord], list[str]]:
    warnings: list[str] = []
    try:
        probe = _run_git(root, ["rev-parse", "--is-inside-work-tree"], limits, 64_000)
    except GitError as exc:
        return [], [str(exc)]
    if probe.strip() != "true":
        return [], ["Git 작업 트리가 아니므로 커밋 추적을 건너뛰었습니다."]

    args = [
        "log",
        f"--max-count={max_commits}",
        f"--since={since}",
        f"--until={until}",
        "--date=iso-strict",
        "--numstat",
        "--format=%x1e%H%x1f%h%x1f%aI%x1f%an%x1f%ae%x1f%P%x1f%D%x1f%s",
        "--no-renames",
    ]
    try:
        activity = _parse_log(_run_git(root, args, limits, limits.git_output_bytes), area_rules)
    except GitError as exc:
        warnings.append(str(exc))
        activity = []

    if activity:
        try:
            _enrich_commit_messages(root, activity, limits)
        except GitError as exc:
            warnings.append(str(exc))
        try:
            _enrich_branch_context(root, activity, limits)
        except GitError as exc:
            warnings.append(str(exc))
        try:
            _enrich_semantic_changes(root, activity, limits)
        except GitError as exc:
            warnings.append(str(exc))

    for commit in activity:
        _map_member(commit, members)

    if include_working_tree:
        try:
            working = _collect_working_tree(root, area_rules, limits)
            if working:
                try:
                    branch = _current_branch(root, limits)
                    if branch:
                        working.branches = [branch]
                    patch = _run_git(root, ["diff", "--unified=0", "--no-renames", "--no-ext-diff", "--no-textconv", "--", "*.h", "*.hpp", "*.cpp", "*.cs", "*.py", "*.js", "*.ts", "*.tsx"], limits, limits.git_output_bytes // 2)
                    cached = _run_git(root, ["diff", "--cached", "--unified=0", "--no-renames", "--no-ext-diff", "--no-textconv", "--", "*.h", "*.hpp", "*.cpp", "*.cs", "*.py", "*.js", "*.ts", "*.tsx"], limits, limits.git_output_bytes // 2)
                    working.semantic_changes, working.semantic_flow = analyze_patch(patch + "\n" + cached)
                except GitError as exc:
                    warnings.append(str(exc))
                _map_member(working, members)
                activity.insert(0, working)
        except GitError as exc:
            warnings.append(str(exc))
    return activity, warnings


def _run_git(root: Path, args: list[str], limits: ScanLimits, output_limit: int) -> str:
    environment = os.environ.copy()
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8"})
    # Trust only this explicit invocation; do not mutate the user's global safe.directory list.
    command = ["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root), *args]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=limits.git_timeout_seconds,
            check=False,
            shell=False,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise GitError("git 실행 파일을 찾지 못해 커밋 추적을 건너뛰었습니다.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"Git 명령이 {limits.git_timeout_seconds}초 안에 끝나지 않아 중단했습니다.") from exc
    if len(completed.stdout) > output_limit:
        raise GitError(f"Git 출력이 안전 제한({output_limit // (1024 * 1024)} MiB)을 초과했습니다.")
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        detail = error[-1][:320] if error else f"exit {completed.returncode}"
        raise GitError(f"Git 분석 실패: {detail}")
    return completed.stdout.decode("utf-8", errors="replace")


def _parse_log(output: str, area_rules) -> list[CommitRecord]:
    commits: list[CommitRecord] = []
    for raw_record in output.split("\x1e"):
        record = raw_record.strip("\r\n")
        if not record:
            continue
        lines = record.splitlines()
        header = lines[0].split("\x1f")
        if len(header) < 8:
            continue
        commit = CommitRecord(
            hash=header[0],
            short_hash=header[1],
            date=header[2],
            author=header[3],
            email=header[4],
            parents=[item for item in header[5].split() if item],
            branches=_parse_refs(header[6]),
            subject="\x1f".join(header[7:]).strip(),
        )
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            binary = parts[0] == "-" or parts[1] == "-"
            path = parts[-1].replace("\\", "/")
            commit.files.append(GitFileChange(
                path=path,
                status="M",
                insertions=0 if binary else _integer(parts[0]),
                deletions=0 if binary else _integer(parts[1]),
                binary=binary,
                area=classify_area(path, area_rules),
                domain=classify_domain(path, Path(path).stem),
            ))
        commits.append(commit)
    return commits


def _enrich_branch_context(root: Path, commits: list[CommitRecord], limits: ScanLimits) -> None:
    tracked = [commit for commit in commits if not commit.working_tree and commit.hash]
    if not tracked:
        return
    output = _run_git(
        root,
        ["name-rev", "--name-only", "--refs=refs/heads/*", "--refs=refs/remotes/*", *[commit.hash for commit in tracked]],
        limits,
        min(limits.git_output_bytes, 2 * 1024 * 1024),
    )
    names = output.splitlines()
    if len(names) != len(tracked):
        return
    for commit, value in zip(tracked, names, strict=False):
        name = value.strip()
        if not name or name == "undefined":
            continue
        branch = re.split(r"[~^]", name, maxsplit=1)[0].removeprefix("remotes/")
        if branch and branch not in commit.branches:
            commit.branches.insert(0, branch)
            commit.branches = commit.branches[:6]


def _enrich_commit_messages(root: Path, commits: list[CommitRecord], limits: ScanLimits) -> None:
    tracked = [commit for commit in commits if not commit.working_tree and commit.hash]
    if not tracked:
        return
    output = _run_git(
        root,
        ["show", "-s", "--format=%x1e%H%x1f%B", *[commit.hash for commit in tracked]],
        limits,
        min(limits.git_output_bytes, 8 * 1024 * 1024),
    )
    by_hash = {commit.hash: commit for commit in tracked}
    for raw in output.split("\x1e"):
        record = raw.lstrip("\r\n")
        if not record or "\x1f" not in record:
            continue
        commit_hash, message = record.split("\x1f", 1)
        commit = by_hash.get(commit_hash.strip())
        if commit is None:
            continue
        lines = message.strip().splitlines()
        if lines and lines[0].strip() == commit.subject.strip():
            lines = lines[1:]
        commit.body = "\n".join(lines).strip()[:16_000]


def _enrich_semantic_changes(root: Path, commits: list[CommitRecord], limits: ScanLimits) -> None:
    source_suffixes = {".h", ".hpp", ".cpp", ".c", ".cc", ".cs", ".py", ".js", ".ts", ".tsx", ".jsx"}
    tracked = [
        commit for commit in commits
        if commit.hash and any(Path(item.path).suffix.casefold() in source_suffixes for item in commit.files)
    ]
    if not tracked:
        return
    output = _run_git(
        root,
        [
            "show", "--format=%x1e%H", "--unified=0", "--no-renames", "--no-ext-diff", "--no-textconv",
            *[commit.hash for commit in tracked], "--",
            ":(glob)**/*.h", ":(glob)**/*.hpp", ":(glob)**/*.cpp", ":(glob)**/*.c", ":(glob)**/*.cc",
            ":(glob)**/*.cs", ":(glob)**/*.py", ":(glob)**/*.js", ":(glob)**/*.ts", ":(glob)**/*.tsx", ":(glob)**/*.jsx",
        ],
        limits,
        limits.git_output_bytes,
    )
    by_hash = {commit.hash: commit for commit in tracked}
    for raw in output.split("\x1e"):
        record = raw.lstrip("\r\n")
        if not record:
            continue
        commit_hash, _, patch = record.partition("\n")
        commit = by_hash.get(commit_hash.strip())
        if commit is None:
            continue
        commit.semantic_changes, commit.semantic_flow = analyze_patch(patch)


def _current_branch(root: Path, limits: ScanLimits) -> str:
    return _run_git(root, ["branch", "--show-current"], limits, 64_000).strip()


def _collect_working_tree(root: Path, area_rules, limits: ScanLimits) -> CommitRecord | None:
    status_output = _run_git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"], limits, limits.git_output_bytes)
    if not status_output:
        return None
    stats: dict[str, tuple[int, int, bool]] = {}
    for args in (["diff", "--numstat", "--no-renames"], ["diff", "--cached", "--numstat", "--no-renames"]):
        for line in _run_git(root, list(args), limits, limits.git_output_bytes // 2).splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            path = parts[-1].replace("\\", "/")
            binary = parts[0] == "-" or parts[1] == "-"
            current = stats.get(path, (0, 0, False))
            stats[path] = (
                current[0] + (0 if binary else _integer(parts[0])),
                current[1] + (0 if binary else _integer(parts[1])),
                current[2] or binary,
            )

    entries = status_output.split("\0")
    changes: list[GitFileChange] = []
    index = 0
    while index < len(entries):
        raw = entries[index]
        index += 1
        if not raw or len(raw) < 4:
            continue
        code = raw[:2]
        path = raw[3:].replace("\\", "/")
        if "R" in code or "C" in code:
            if index < len(entries) and entries[index]:
                path = entries[index].replace("\\", "/")
                index += 1
        insertions, deletions, binary = stats.get(path, (0, 0, Path(path).suffix.casefold() in {".uasset", ".umap", ".png", ".fbx"}))
        changes.append(GitFileChange(
            path=path,
            status=code.strip() or "M",
            insertions=insertions,
            deletions=deletions,
            binary=binary,
            area=classify_area(path, area_rules),
            domain=classify_domain(path, Path(path).stem),
        ))
    if not changes:
        return None
    now = datetime.now(UTC).isoformat()
    return CommitRecord(
        hash="working-tree",
        short_hash="WORKTREE",
        author=os.environ.get("USERNAME") or os.environ.get("USER") or "Local",
        email="",
        date=now,
        subject="커밋되지 않은 로컬 변경",
        files=changes,
        working_tree=True,
    )


def _map_member(commit: CommitRecord, members: list[TeamMember]) -> None:
    haystack = f"{commit.author} {commit.email}".casefold()
    for member in members:
        candidates = [member.name, *member.aliases]
        if any(candidate.casefold() in haystack for candidate in candidates if candidate):
            commit.member = member.name
            commit.role = member.role
            return
    commit.member = commit.author or "Unknown"
    commit.role = "Unmapped"


def _parse_refs(value: str) -> list[str]:
    result = []
    for item in value.split(","):
        clean = item.strip().replace("HEAD -> ", "")
        if clean and not clean.startswith("tag:"):
            result.append(clean)
    return result[:12]


def _parse_date(value: str, end_of_day: bool) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"날짜 형식이 잘못되었습니다: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if len(value.strip()) == 10 and end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed.astimezone(UTC)


def _integer(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0
