from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from .analyzer import build_report, load_previous_state
from .config import ConfigError, load_project_context, load_repositories, parse_repository_argument
from .git_tracker import collect_git_activity, resolve_period
from .local_review import backfill_existing_reviews
from .models import RepositoryConfig, ScanLimits
from .reporter import project_slug, write_portfolio, write_project_artifacts
from .scanner import IGNORED_DIRECTORIES, scan_project
from .server import serve_directory


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    try:
        if args.command == "scan":
            return run_scan(args)
        if args.command == "watch":
            return run_watch(args)
        if args.command == "serve":
            return run_serve(args)
        if args.command == "self-test":
            return run_self_test(args)
    except (ConfigError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n중단했습니다.")
        return 130
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="work-tracker",
        description="코드 구조, Git 활동, 변경 스냅샷과 위험 신호를 통합하는 로컬 프로젝트 추적기",
    )
    subparsers = parser.add_subparsers(dest="command")
    scan = subparsers.add_parser("scan", help="프로젝트를 분석하고 통합 대시보드를 생성합니다.")
    _add_scan_arguments(scan)
    watch = subparsers.add_parser("watch", help="프로젝트 변경을 감시하고 결과를 갱신합니다.")
    _add_scan_arguments(watch)
    watch.add_argument("--interval", type=float, default=5.0, help="변경 확인 간격(초). 기본값 5")
    serve = subparsers.add_parser("serve", help="생성된 대시보드를 안전한 로컬 서버로 제공합니다.")
    _add_server_arguments(serve)
    self_test = subparsers.add_parser("self-test", help="스캔·비교·산출물 파이프라인을 자체 검증합니다.")
    self_test.add_argument("--output-dir", default="output", help="테스트 산출물 루트")
    return parser


def _add_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", "-p", action="append", default=[], metavar="[NAME=]PATH", help="분석할 프로젝트. 여러 번 지정할 수 있습니다.")
    parser.add_argument("--name", "-n", help="프로젝트 하나를 지정했을 때 표시 이름")
    parser.add_argument("--config-dir", default="examples", help="repos/team/ownership 설정 폴더. 기본값 examples")
    parser.add_argument("--output-dir", "-o", default="output", help="출력 루트. 기본값 output")
    parser.add_argument("--days", type=int, help="최근 N일의 Git 활동만 분석합니다. 기본값은 initial commit부터 전체 이력")
    parser.add_argument("--since", help="Git 활동 시작 시각 또는 날짜")
    parser.add_argument("--until", help="Git 활동 종료 시각 또는 날짜")
    parser.add_argument("--include-working-tree", action=argparse.BooleanOptionalAction, default=True, help="커밋되지 않은 변경 포함")
    parser.add_argument("--max-commits", type=int, help="프로젝트별 최대 커밋 수. 기본값은 제한 없음")
    parser.add_argument("--max-files", type=int, default=100_000, help="프로젝트별 최대 추적 파일 수")
    parser.add_argument("--quiet", action="store_true", help="진행 로그 최소화")
    parser.add_argument(
        "--backfill-existing-reviews",
        action="store_true",
        help="현재까지의 커밋을 외부 API 없이 기존 이력 분석으로 채웁니다. 이후 새 커밋에는 적용되지 않습니다.",
    )


def _add_server_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-dir", "-o", default="output", help="대시보드 출력 루트")
    parser.add_argument("--host", default="127.0.0.1", help="바인딩 호스트. 기본값 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="시작 포트. 기본값 8765")
    parser.add_argument("--allow-remote", action="store_true", help="루프백 외부 바인딩을 명시적으로 허용")
    parser.add_argument("--env-file", default=".env", help="OpenAI API 설정 파일. 기본값 .env")
    parser.add_argument("--quiet", action="store_true", help="요청 로그 숨김")


def run_scan(args: argparse.Namespace) -> int:
    repositories, context, output_root, limits, since, until = _prepare_scan(args)
    reports = _scan_all(repositories, context, output_root, limits, since, until, args)
    if not reports:
        raise ValueError("분석 가능한 프로젝트가 없습니다.")
    write_portfolio(output_root, reports)
    if not args.quiet:
        print(f"\n완료: {output_root / 'index.html'}")
    return 0


def run_watch(args: argparse.Namespace) -> int:
    if args.interval < 1:
        raise ValueError("--interval은 1초 이상이어야 합니다.")
    repositories, context, output_root, limits, since, until = _prepare_scan(args)
    reports = _scan_all(repositories, context, output_root, limits, since, until, args)
    if not reports:
        raise ValueError("감시할 수 있는 프로젝트가 없습니다.")
    write_portfolio(output_root, reports)
    signatures = {repo.path: _quick_signature(Path(repo.path), output_root, limits.max_files) for repo in repositories if Path(repo.path).is_dir()}
    print(f"[watch] {len(signatures)}개 프로젝트 · {args.interval:g}초 간격 · Ctrl+C로 중지")
    while True:
        time.sleep(args.interval)
        dirty: list[RepositoryConfig] = []
        for repo in repositories:
            root = Path(repo.path)
            if not root.is_dir():
                continue
            signature = _quick_signature(root, output_root, limits.max_files)
            if signature != signatures.get(repo.path):
                signatures[repo.path] = signature
                dirty.append(repo)
        if not dirty:
            continue
        print(f"[watch] 변경 감지: {', '.join(item.name for item in dirty)}")
        scan_since, scan_until = resolve_period(args.since, args.until, args.days)
        updated = _scan_all(dirty, context, output_root, limits, scan_since, scan_until, args)
        by_name = {item.project_name: item for item in reports}
        by_name.update({item.project_name: item for item in updated})
        reports = list(by_name.values())
        write_portfolio(output_root, reports)


def run_serve(args: argparse.Namespace) -> int:
    serve_directory(Path(args.output_dir), args.host, args.port, args.allow_remote, args.quiet, Path(args.env_file))
    return 0


def run_self_test(args: argparse.Namespace) -> int:
    root = Path(args.output_dir).resolve() / "_selftest_v2"
    fixture = root / "fixture" / "WarZMini"
    generated = root / "result"
    if root.exists():
        shutil.rmtree(root)
    _create_fixture(fixture)
    limits = ScanLimits(max_files=5_000)
    context = load_project_context(Path("__no_config__"))
    since, until = resolve_period(None, None, 30)
    first_inventory = scan_project(fixture, [], limits, [generated])
    first_report = build_report("WarZMini", fixture, generated / "projects" / "warzmini", since, until, first_inventory, [], context, None)
    write_project_artifacts(first_report)
    source = fixture / "Source" / "WarZMini" / "WarZMiniCharacter.cpp"
    source.write_text(source.read_text(encoding="utf-8") + "\nvoid AWarZMiniCharacter::Reload() {}\n", encoding="utf-8")
    config = fixture / "Config" / "DefaultInput.ini"
    config.write_text("[/Script/EnhancedInput.EnhancedInputDeveloperSettings]\nbEnableUserSettings=True\n", encoding="utf-8")
    previous = load_previous_state(Path(first_report.output_path) / "state.json")
    second_inventory = scan_project(fixture, [], limits, [generated])
    second_report = build_report("WarZMini", fixture, Path(first_report.output_path), since, until, second_inventory, [], context, previous)
    write_project_artifacts(second_report)
    write_portfolio(generated, [second_report])
    failures = []
    if not any(item.name == "AWarZMiniCharacter" for item in second_report.classes):
        failures.append("Unreal 클래스를 추출하지 못했습니다.")
    if len(second_report.changes) < 2:
        failures.append("추가·수정 파일 비교가 동작하지 않습니다.")
    for name in ("index.html", "report.json", "summary.md", "architecture.mmd", "activity.csv", "risks.csv", "state.json", "history.jsonl"):
        if not (Path(second_report.output_path) / name).exists():
            failures.append(f"산출물이 없습니다: {name}")
    html_text = (Path(second_report.output_path) / "index.html").read_text(encoding="utf-8")
    if "WorkTracker" not in html_text or "Content-Security-Policy" not in html_text:
        failures.append("대시보드 또는 보안 정책이 생성되지 않았습니다.")
    if failures:
        print("[self-test] 실패")
        for failure in failures:
            print("  - " + failure)
        return 1
    print("[self-test] 통과")
    print(f"  classes : {len(second_report.classes)}")
    print(f"  changes : {len(second_report.changes)}")
    print(f"  output  : {generated / 'index.html'}")
    return 0


def _prepare_scan(args: argparse.Namespace):
    config_dir = Path(args.config_dir).resolve()
    output_root = Path(args.output_dir).resolve()
    context = load_project_context(config_dir)
    repositories = [parse_repository_argument(value) for value in args.project] if args.project else load_repositories(config_dir)
    if args.name:
        if len(repositories) != 1:
            raise ValueError("--name은 프로젝트를 하나만 지정했을 때 사용할 수 있습니다.")
        repositories[0].name = args.name.strip()
    if not repositories:
        raise ValueError("--project를 지정하거나 config-dir에 repos.yaml을 추가하세요.")
    slugs = [project_slug(repo.name) for repo in repositories]
    if len(slugs) != len(set(slugs)):
        raise ValueError("프로젝트 이름을 안전한 경로로 변환한 결과가 중복됩니다. 서로 다른 이름을 지정하세요.")
    if (args.days is not None and args.days < 1) or (args.max_commits is not None and args.max_commits < 1) or args.max_files < 100:
        raise ValueError("--days/--max-commits는 지정할 경우 양수이고 --max-files는 100 이상이어야 합니다.")
    since, until = resolve_period(args.since, args.until, args.days)
    limits = ScanLimits(max_files=args.max_files)
    return repositories, context, output_root, limits, since, until


def _scan_all(repositories, context, output_root, limits, since, until, args):
    reports = []
    for repo in repositories:
        root = Path(repo.path).resolve()
        if not root.is_dir():
            print(f"경고: 프로젝트를 찾을 수 없어 건너뜁니다: {repo.name} ({root})", file=sys.stderr)
            continue
        project_output = output_root / "projects" / project_slug(repo.name)
        if not args.quiet:
            print(f"[scan] {repo.name}")
            print(f"       project: {root}")
        inventory = scan_project(root, context.areas, limits, [output_root, project_output])
        commits, git_warnings = collect_git_activity(
            root, since, until, context.members, context.areas, limits,
            args.include_working_tree, args.max_commits,
        )
        inventory.warnings.extend(item for item in git_warnings if item not in inventory.warnings)
        previous = load_previous_state(project_output / "state.json")
        committed_dates = [item.date for item in commits if not item.working_tree]
        report_since = min(committed_dates) if args.since is None and args.days is None and committed_dates else since
        report = build_report(repo.name, root, project_output, report_since, until, inventory, commits, context, previous)
        if args.backfill_existing_reviews:
            filled = backfill_existing_reviews(report.commits)
            if filled and not args.quiet:
                print(f"       baseline reviews: {filled:,}개")
        write_project_artifacts(report)
        reports.append(report)
        if not args.quiet:
            print(f"       files: {len(report.files):,} · classes: {len(report.classes):,} · commits: {report.summary['commits']:,}")
            print(f"       delta: {len(report.changes):,} · risks: {len(report.risks):,} · {project_output / 'index.html'}")
    return reports


def _quick_signature(root: Path, output_root: Path, max_files: int) -> tuple[int, int, int, int]:
    stack = [root.resolve()]
    output = output_root.resolve()
    count = 0
    newest = 0
    mixed = 0
    ignored = {item.casefold() for item in IGNORED_DIRECTORIES}
    while stack and count < max_files:
        directory = stack.pop()
        try:
            directory.relative_to(output)
            continue
        except ValueError:
            pass
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.casefold() not in ignored:
                        stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    info = entry.stat(follow_symlinks=False)
                    count += 1
                    newest = max(newest, info.st_mtime_ns)
                    mixed ^= hash((entry.path, info.st_size, info.st_mtime_ns))
            except OSError:
                continue
    return count, newest, mixed, _git_metadata_signature(root)


def _git_metadata_signature(root: Path) -> int:
    """Track Git state without walking object storage or reading source content."""
    git_dir = _find_git_directory(root)
    if git_dir is None:
        return 0
    candidates = [git_dir / "HEAD", git_dir / "index", git_dir / "packed-refs", git_dir / "FETCH_HEAD"]
    refs = git_dir / "refs"
    if refs.is_dir():
        try:
            candidates.extend(path for index, path in enumerate(refs.rglob("*")) if index < 5_000 and path.is_file())
        except OSError:
            pass
    mixed = hash(str(git_dir))
    for path in candidates:
        try:
            info = path.stat()
            mixed ^= hash((str(path.relative_to(git_dir)), info.st_size, info.st_mtime_ns))
        except (OSError, ValueError):
            continue
    return mixed


def _find_git_directory(root: Path) -> Path | None:
    current = root.resolve()
    for parent in (current, *current.parents):
        marker = parent / ".git"
        if marker.is_dir():
            return marker.resolve()
        if marker.is_file():
            try:
                line = marker.read_text(encoding="utf-8", errors="replace")[:4_096].strip()
            except OSError:
                continue
            if line.casefold().startswith("gitdir:"):
                target = Path(line.split(":", 1)[1].strip())
                return (target if target.is_absolute() else parent / target).resolve()
    return None


def _create_fixture(root: Path) -> None:
    source = root / "Source" / "WarZMini"
    source.mkdir(parents=True, exist_ok=True)
    (root / "Config").mkdir(parents=True, exist_ok=True)
    (root / "Content" / "Blueprints").mkdir(parents=True, exist_ok=True)
    (root / "WarZMini.uproject").write_text('{"FileVersion":3,"Modules":[{"Name":"WarZMini","Type":"Runtime"}]}', encoding="utf-8")
    (source / "WarZMini.Build.cs").write_text('PublicDependencyModuleNames.AddRange(new string[] { "Core", "Engine", "EnhancedInput" });', encoding="utf-8")
    (source / "WarZMiniCharacter.h").write_text('''#pragma once
#include "GameFramework/Character.h"
UCLASS()
class WARZMINI_API AWarZMiniCharacter : public ACharacter
{
public:
    UPROPERTY(VisibleAnywhere, Category="Combat")
    TObjectPtr<UHealthComponent> HealthComponent;
    UFUNCTION(BlueprintCallable)
    void FireWeapon();
};
''', encoding="utf-8")
    (source / "WarZMiniCharacter.cpp").write_text('''#include "WarZMiniCharacter.h"
AWarZMiniCharacter::AWarZMiniCharacter() { HealthComponent = CreateDefaultSubobject<UHealthComponent>(TEXT("Health")); }
void AWarZMiniCharacter::FireWeapon() {}
''', encoding="utf-8")
    (root / "Config" / "DefaultGame.ini").write_text("[/Script/EngineSettings.GeneralProjectSettings]\nProjectName=WarZMini\n", encoding="utf-8")
    (root / "Content" / "Blueprints" / "BP_WarZMiniCharacter.uasset").write_bytes(b"fake-binary-asset")
