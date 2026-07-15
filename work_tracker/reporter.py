from __future__ import annotations

import csv
import html
import io
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .analyzer import activity_by_member
from .models import ProjectReport
from .scanner import domain_title


def project_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9가-힣]+", "-", value.casefold()).strip("-")
    return slug or "project"


def write_project_artifacts(report: ProjectReport) -> None:
    output = Path(report.output_path)
    output.mkdir(parents=True, exist_ok=True)
    (output / "snapshots").mkdir(parents=True, exist_ok=True)
    payload = report.as_dict()
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    _atomic_write(output / "report.json", json_text)
    _atomic_write(output / "state.json", json_text)
    _atomic_write(output / "snapshots" / f"{report.run_id}.json", json_text)
    _write_history(output / "history.jsonl", report)
    _atomic_write(output / "summary.md", render_markdown(report))
    _atomic_write(output / "architecture.mmd", render_mermaid(report))
    _atomic_write(output / "activity.csv", render_activity_csv(report))
    _atomic_write(output / "risks.csv", render_risks_csv(report))
    _atomic_write(output / "index.html", render_dashboard(report))


def write_portfolio(output_root: Path, reports: list[ProjectReport]) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    projects = []
    for report in reports:
        projects.append({
            "name": report.project_name,
            "href": f"projects/{project_slug(report.project_name)}/index.html",
            "created_at": report.created_at,
            "summary": report.summary,
            "milestone": report.milestone,
            "top_domains": [item.title for item in report.domains[:4]],
            "risks": [{"severity": item.severity, "title": item.title} for item in report.risks[:4]],
        })
    _atomic_write(output_root / "portfolio.json", json.dumps({"schema_version": "2.2", "projects": projects}, ensure_ascii=False, indent=2))
    _atomic_write(output_root / "index.html", render_portfolio(projects))


def render_markdown(report: ProjectReport) -> str:
    summary = report.summary
    high_risks = [item for item in report.risks if item.severity in {"critical", "high"}]
    lines = [
        f"# {report.project_name} · WorkTracker",
        "",
        f"> 생성: {report.created_at}  ",
        f"> 기간: {report.period_since} — {report.period_until}  ",
        f"> 프로젝트: `{report.project_path}`",
        "",
        "## 한눈에 보기",
        "",
        f"- 추적 파일: **{summary['files']:,}**",
        f"- 모듈 / 클래스 / 관계: **{summary['modules']} / {summary['classes']} / {summary['relationships']}**",
        f"- 기간 내 커밋: **{summary['commits']}**",
        f"- 이전 스캔 이후 변경: **{summary['changes']}**",
        f"- 주의 필요: **{len(high_risks)}** / 전체 신호 {summary['risks']}",
    ]
    if report.milestone:
        lines.extend(["", "## 현재 마일스톤", "", report.milestone])
    lines.extend(["", "## 우선 확인", ""])
    for risk in report.risks:
        lines.append(f"### [{risk.severity.upper()}] {risk.title}")
        lines.append("")
        lines.append(risk.detail)
        if risk.evidence:
            lines.extend(["", *[f"- `{item}`" for item in risk.evidence]])
        if risk.action:
            lines.extend(["", f"**권장:** {risk.action}"])
        lines.append("")
    lines.extend(["## 도메인", ""])
    for domain in report.domains:
        lines.append(f"### {domain.title}")
        lines.append("")
        lines.append(domain.summary)
        lines.append(f"- 클래스 {len(domain.classes)} · 컴포넌트 {len(domain.components)} · 파일 {len(domain.files)}")
        if domain.classes:
            lines.append("- 주요 클래스: " + ", ".join(f"`{item}`" for item in domain.classes[:12]))
        lines.append("")
    lines.extend(["## 최근 활동", ""])
    for commit in report.commits[:40]:
        marker = "WORKTREE" if commit.working_tree else commit.short_hash
        branch = commit.branches[0] if commit.branches else "미확인"
        lines.append(f"- **{marker}** · {commit.date[:10]} · {commit.member} · `{branch}` · {commit.subject} ({len(commit.files)} files, +{commit.insertions}/-{commit.deletions})")
        for point in commit.review.pr_summary:
            lines.append(f"  - {point}")
        if commit.review.summary:
            lines.append(f"  - 변경 범위: {commit.review.summary}")
    lines.extend(["", "## 이전 스캔 이후 파일 변경", ""])
    if not report.changes:
        lines.append("- 기준 스냅샷이거나 파일 단위 변경이 없습니다.")
    else:
        for change in report.changes[:200]:
            lines.append(f"- **{change.status}** `{change.path}` · {change.meaning}")
    if report.warnings:
        lines.extend(["", "## 수집 경고", "", *[f"- {item}" for item in report.warnings]])
    return "\n".join(lines).rstrip() + "\n"


def render_mermaid(report: ProjectReport) -> str:
    lines = ["flowchart LR", f"  ROOT[\"{_mermaid_text(report.project_name)}\"]"]
    known: dict[str, str] = {}
    for index, domain in enumerate(report.domains):
        domain_id = f"D{index}"
        lines.append(f"  {domain_id}[\"{_mermaid_text(domain.title)}\\n{len(domain.classes)} classes\"]")
        lines.append(f"  ROOT --> {domain_id}")
        for class_index, name in enumerate(domain.classes[:12]):
            node_id = f"{domain_id}C{class_index}"
            known[name] = node_id
            lines.append(f"  {node_id}[\"{_mermaid_text(name)}\"]")
            lines.append(f"  {domain_id} --> {node_id}")
    for edge in report.relationships:
        if edge.source in known and edge.target in known:
            lines.append(f"  {known[edge.source]} -- \"{_mermaid_text(edge.kind)}\" --> {known[edge.target]}")
    return "\n".join(lines) + "\n"


def render_activity_csv(report: ProjectReport) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["hash", "date", "member", "role", "author", "branches", "subject", "files", "insertions", "deletions", "domains", "analysis_status", "analysis_model", "change_type", "impact", "pr_summary", "review_summary", "working_tree"])
    for commit in report.commits:
        domains = Counter(file.domain for file in commit.files)
        writer.writerow([
            commit.short_hash, commit.date, commit.member, commit.role, commit.author, "; ".join(commit.branches), commit.subject,
            len(commit.files), commit.insertions, commit.deletions,
            "; ".join(f"{name}:{count}" for name, count in domains.most_common()),
            commit.review.status, commit.review.model, commit.review.change_type, commit.review.impact, " | ".join(commit.review.pr_summary), commit.review.summary,
            str(commit.working_tree).lower(),
        ])
    return stream.getvalue()


def render_risks_csv(report: ProjectReport) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["id", "severity", "title", "detail", "evidence", "action"])
    for risk in report.risks:
        writer.writerow([risk.id, risk.severity, risk.title, risk.detail, " | ".join(risk.evidence), risk.action])
    return stream.getvalue()


def render_dashboard(report: ProjectReport) -> str:
    data = report.as_dict()
    data["project_slug"] = project_slug(report.project_name)
    for payload, commit in zip(data["commits"], report.commits, strict=False):
        payload["insertions"] = commit.insertions
        payload["deletions"] = commit.deletions
    data["ui_limits"] = {
        "files": min(len(report.files), 10_000),
        "classes": min(len(report.classes), 1_500),
        "relationships": min(len(report.relationships), 4_000),
        "commits": min(len(report.commits), 400),
        "changes": min(len(report.changes), 5_000),
    }
    data["files"] = data["files"][:10_000]
    data["classes"] = data["classes"][:1_500]
    data["relationships"] = data["relationships"][:4_000]
    data["commits"] = data["commits"][:400]
    data["changes"] = data["changes"][:5_000]
    data["team_activity"] = activity_by_member(report.commits)
    json_data = _json_for_script(data)
    title = html.escape(f"{report.project_name} · WorkTracker")
    return (
        DASHBOARD_TEMPLATE
        .replace("__TITLE__", title)
        .replace("__DASHBOARD_CSS__", DASHBOARD_CSS)
        .replace("__REPORT_JSON__", json_data)
        .replace("__DASHBOARD_JS__", DASHBOARD_JS)
    )


def render_portfolio(projects: list[dict[str, Any]]) -> str:
    cards = []
    for project in projects:
        summary = project["summary"]
        risks = project["risks"]
        highest = risks[0]["severity"] if risks else "info"
        cards.append(f"""
        <a class="project-card" href="{html.escape(project['href'], quote=True)}">
          <div class="project-card__top"><span class="status-dot {html.escape(highest)}"></span><span>{html.escape(project['created_at'][:16].replace('T', ' '))}</span></div>
          <h2>{html.escape(project['name'])}</h2>
          <p>{html.escape(project['milestone'] or '마일스톤 미설정')}</p>
          <div class="project-stats">
            <span><strong>{summary['commits']}</strong> 커밋</span><span><strong>{summary['changes']}</strong> 변경</span><span><strong>{summary['risks']}</strong> 신호</span>
          </div>
          <div class="project-domains">{''.join(f'<span>{html.escape(item)}</span>' for item in project['top_domains'])}</div>
        </a>""")
    body = "".join(cards) or '<div class="empty"><h2>분석 결과가 없습니다</h2><p>scan 명령으로 프로젝트를 먼저 분석하세요.</p></div>'
    return (
        PORTFOLIO_TEMPLATE
        .replace("__PORTFOLIO_CSS__", PORTFOLIO_CSS)
        .replace("__PROJECT_CARDS__", body)
        .replace("__PROJECT_COUNT__", str(len(projects)))
    )


def _write_history(path: Path, report: ProjectReport) -> None:
    entries: list[dict[str, Any]] = []
    try:
        if path.exists() and path.stat().st_size <= 8 * 1024 * 1024:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        entries.append(value)
                except json.JSONDecodeError:
                    continue
    except OSError:
        entries = []
    entries.append({
        "run_id": report.run_id,
        "created_at": report.created_at,
        "project_name": report.project_name,
        "summary": report.summary,
        "risk_counts": dict(Counter(item.severity for item in report.risks)),
        "change_counts": dict(Counter(item.status for item in report.changes)),
    })
    text = "\n".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in entries[-300:]) + "\n"
    _atomic_write(path, text)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _json_for_script(value: Any) -> str:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _mermaid_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', "'").replace("\n", " ")[:100]


_TEMPLATE_DIRECTORY = Path(__file__).with_name("templates")
_ASSET_DIRECTORY = Path(__file__).with_name("assets")


def _read_web_asset(directory: Path, name: str) -> str:
    return (directory / name).read_text(encoding="utf-8")


DASHBOARD_TEMPLATE = _read_web_asset(_TEMPLATE_DIRECTORY, "dashboard.html")
DASHBOARD_CSS = _read_web_asset(_ASSET_DIRECTORY, "dashboard.css")
DASHBOARD_JS = _read_web_asset(_ASSET_DIRECTORY, "dashboard.js")
PORTFOLIO_TEMPLATE = _read_web_asset(_TEMPLATE_DIRECTORY, "portfolio.html")
PORTFOLIO_CSS = _read_web_asset(_ASSET_DIRECTORY, "portfolio.css")
