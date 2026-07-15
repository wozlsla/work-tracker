from __future__ import annotations

import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

from work_tracker.analyzer import build_report
from work_tracker.models import ProjectContext
from work_tracker.reporter import render_dashboard, render_portfolio
from work_tracker.scanner import ScanInventory


class _ContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.external: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if "id" in values:
            self.ids.append(values["id"])
        for key in ("src", "href"):
            value = values.get(key, "")
            if value.startswith(("http://", "https://", "//")):
                self.external.append(value)


class DashboardContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report = build_report(
            "Demo", Path("."), Path("output"),
            "2026-07-01T00:00:00+00:00", "2026-07-15T00:00:00+00:00",
            ScanInventory(), [], ProjectContext(), None,
        )
        self.html = render_dashboard(self.report)

    def test_dom_ids_are_unique_and_script_references_exist(self) -> None:
        parser = _ContractParser()
        parser.feed(self.html)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        references = set(re.findall(r"\$\('([^']+)'\)", self.html))
        self.assertFalse(references - set(parser.ids), references - set(parser.ids))

    def test_dashboard_has_no_external_resources(self) -> None:
        parser = _ContractParser()
        parser.feed(self.html)
        self.assertEqual(parser.external, [])
        self.assertIn("Content-Security-Policy", self.html)
        self.assertIn("@media(max-width:780px)", self.html)
        self.assertIn("prefers-reduced-motion", self.html)

    def test_embedded_report_is_valid_json(self) -> None:
        match = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', self.html, re.DOTALL)
        self.assertIsNotNone(match)
        payload = json.loads(match.group(1))
        self.assertEqual(payload["project_name"], "Demo")
        self.assertIn("ui_limits", payload)

    def test_architecture_has_draggable_graph_and_system_map(self) -> None:
        self.assertIn('data-architecture-mode="relations"', self.html)
        self.assertIn('data-architecture-mode="map"', self.html)
        self.assertIn('id="systemMap"', self.html)
        self.assertIn('id="mapZoomIn"', self.html)
        self.assertIn('id="mapZoomOut"', self.html)
        self.assertIn("addEventListener('pointerdown'", self.html)
        self.assertIn("setPointerCapture", self.html)
        self.assertIn("saveGraphPositions", self.html)
        self.assertIn("renderSystemMap", self.html)

    def test_architecture_has_group_selection_alignment_and_edge_weight(self) -> None:
        self.assertIn('id="graphEdgeWeight"', self.html)
        self.assertIn('id="graphAlignMenu"', self.html)
        self.assertIn("selection-marquee", self.html)
        self.assertIn("graphSelection=new Set", self.html)
        self.assertIn("alignGraphSelection", self.html)
        self.assertIn("workTrackerEdgeWeight", self.html)

    def test_dashboard_has_composable_theme_system(self) -> None:
        self.assertIn('id="themeSettings"', self.html)
        self.assertIn('data-theme-mode="system"', self.html)
        self.assertIn('data-style="standard"', self.html)
        self.assertIn('data-theme-style="material"', self.html)
        self.assertIn('data-theme-base="slate"', self.html)
        self.assertIn('data-theme-base="neutral"', self.html)
        self.assertIn('data-theme-base="material-ocean"', self.html)
        self.assertIn('data-theme-base="material-palenight"', self.html)
        self.assertIn('data-theme-accent="violet"', self.html)
        self.assertIn('data-theme-accent="material"', self.html)
        self.assertIn('data-theme-font="pretendard"', self.html)
        self.assertIn("oklch(", self.html)
        self.assertIn("workTrackerThemeMode", self.html)
        self.assertIn("workTrackerThemeBase", self.html)
        self.assertIn("workTrackerThemeAccent", self.html)
        self.assertIn("workTrackerFont", self.html)
        self.assertIn("workTrackerThemeStyle", self.html)
        self.assertIn('html[data-style="material"]', self.html)

    def test_native_select_options_follow_active_theme(self) -> None:
        self.assertIn('html[data-theme="dark"] select{color-scheme:dark}', self.html)
        self.assertIn("select option:checked", self.html)
        self.assertIn("background-color:var(--surface)!important", self.html)

    def test_overview_is_feed_based_and_sidebar_is_collapsible(self) -> None:
        self.assertIn('id="projectFeed"', self.html)
        self.assertIn('id="overviewMeta"', self.html)
        self.assertIn('id="sidebarToggle"', self.html)
        self.assertIn('class="project-home"', self.html)
        self.assertIn('href="../../index.html"', self.html)
        self.assertIn("workTrackerSidebarCollapsed", self.html)
        self.assertNotIn('id="heroTitle"', self.html)
        self.assertNotIn("gradient(", self.html)

    def test_architecture_is_a_bounded_scrolling_workbench(self) -> None:
        self.assertIn("height:clamp(700px,calc(100vh - 170px),920px)", self.html)
        self.assertIn("overflow-y:auto", self.html)
        self.assertIn("overscroll-behavior:contain", self.html)
        self.assertIn('id="architectureResizer"', self.html)
        self.assertIn('role="separator"', self.html)
        self.assertIn('aria-controls="inspector"', self.html)
        self.assertIn("Home:range.min,End:range.max", self.html)
        self.assertIn("aria-valuetext", self.html)
        self.assertIn("workTrackerInspectorWidth", self.html)

    def test_architecture_uses_the_live_panel_bounds(self) -> None:
        self.assertIn("svg.clientWidth||shell.clientWidth", self.html)
        self.assertIn("svg.clientHeight||shell.clientHeight", self.html)
        self.assertIn("viewportWidth/graphScale", self.html)
        self.assertIn("ResizeObserver", self.html)
        self.assertIn(".system-map-shell #mapStatus{position:absolute", self.html)

    def test_relationship_nodes_use_an_unbounded_world_after_initial_layout(self) -> None:
        self.assertIn("const WORLD_LIMIT=1000000", self.html)
        self.assertIn("item.x=worldCoordinate(origin.x+dx)", self.html)
        self.assertIn("saved?worldCoordinate(saved.x,initialX)", self.html)
        self.assertIn("node.x=worldCoordinate(node.x);node.y=worldCoordinate(node.y)", self.html)
        self.assertNotIn("item.x=Math.max(minX,Math.min(maxX,origin.x+dx))", self.html)
        self.assertNotIn("node.x=Math.max(55,Math.min(graphRuntime.width-55,node.x))", self.html)

    def test_architecture_uses_right_drag_camera_without_canvas_scrollbars(self) -> None:
        self.assertIn("function initCanvasNavigation", self.html)
        self.assertIn("removeEventListener('wheel',handleGraphWheel)", self.html)
        self.assertIn("removeEventListener('wheel',handleMapWheel)", self.html)
        self.assertIn("shell.addEventListener('wheel'", self.html)
        self.assertIn("setGraphScale(graphScale+step,anchor)", self.html)
        self.assertIn("setMapScale(mapScale+step,anchor)", self.html)
        self.assertIn("if(event.button!==2)return", self.html)
        self.assertIn("graphPan.x=drag.origin.x+dx", self.html)
        self.assertIn("mapPan.x=drag.origin.x+dx", self.html)
        self.assertIn(".system-map-shell{overflow:hidden}", self.html)
        self.assertIn("function setMapScale", self.html)
        self.assertIn("content.setAttribute('transform','translate('+mapPan.x+' '+mapPan.y+') scale('+mapScale+')')", self.html)

    def test_activity_expands_pr_style_commit_review(self) -> None:
        self.assertIn("function renderCommitReview", self.html)
        self.assertIn("function renderPrBody", self.html)
        self.assertIn("AI 구현 분석", self.html)
        self.assertIn("OpenAI로 분석", self.html)
        self.assertIn("codex-backfill", self.html)
        self.assertIn("OpenAI로 다시 분석", self.html)
        self.assertIn("function requestCommitReview", self.html)
        self.assertIn("/api/ai-review", self.html)
        self.assertIn("X-WorkTracker-Request", self.html)
        self.assertIn("function hydrateSavedReviews", self.html)
        self.assertIn("fetch('report.json'", self.html)
        self.assertIn("핵심 구조", self.html)
        self.assertIn("변경 내용", self.html)
        self.assertIn("네트워크 흐름", self.html)
        self.assertIn("구현 내용", self.html)
        self.assertIn("잠재 위험", self.html)
        self.assertIn("검증 제안", self.html)
        self.assertIn("document.createElement('details')", self.html)
        self.assertIn("review-evidence-count", self.html)
        self.assertIn(".review-evidence:not([open]) .review-evidence-list{display:none}", self.html)
        self.assertIn("commit-branch", self.html)
        self.assertIn("item.branches", self.html)
        self.assertIn("searchable=[item.short_hash,item.hash,item.subject,item.body", self.html)
        self.assertNotIn("item.files.slice(0,3)", self.html)
        self.assertIn("toggle.setAttribute('aria-expanded'", self.html)
        self.assertIn("toggle.dataset.commitHash", self.html)
        self.assertIn("item.review", self.html)

    def test_activity_disclosure_is_stable_and_readable(self) -> None:
        self.assertIn("body{font-size:15px}", self.html)
        self.assertIn(".commit-list{overflow-anchor:none}", self.html)
        self.assertIn(".commit-toggle{min-height:84px", self.html)
        self.assertIn("svgNode('svg',{class:'commit-chevron'", self.html)
        self.assertIn("setView(['overview','activity','architecture','changes','risks','files'].includes(requested)?requested:'overview',false)", self.html)
        self.assertIn("review.source!==current.source", self.html)
        self.assertIn("toggle.setAttribute('aria-controls',panelId)", self.html)

    def test_dashboard_has_keyboard_command_palette(self) -> None:
        self.assertIn('id="commandPalette"', self.html)
        self.assertIn('id="commandSearch"', self.html)
        self.assertIn('data-command="view:architecture"', self.html)
        self.assertIn('data-command="appearance"', self.html)
        self.assertIn("function initCommandPalette", self.html)
        self.assertIn("event.key.toLocaleLowerCase()==='k'", self.html)
        self.assertIn("event.key==='ArrowDown'", self.html)
        self.assertIn("role=\"listbox\"", self.html)
        self.assertIn(':focus-visible{outline:2px solid var(--mint)', self.html)

    def test_sidebar_uses_outline_icons_and_changes_explains_snapshots(self) -> None:
        self.assertIn('class="nav-icon" aria-hidden="true"><svg', self.html)
        self.assertIn('data-view="changes" title="Snapshots"', self.html)
        self.assertIn("Git 커밋과 별개로 직전 스캔과 현재 작업 디렉터리를 비교합니다", self.html)
        self.assertIn("Activity와의 차이", self.html)

    def test_system_map_class_nodes_have_more_vertical_weight(self) -> None:
        self.assertIn("classStep=42,classHeight=34", self.html)
        self.assertIn("height:classHeight", self.html)

    def test_architecture_has_persistent_region_annotations(self) -> None:
        self.assertIn('id="annotationAdd"', self.html)
        self.assertIn('id="annotationWorkspace"', self.html)
        self.assertIn("workTrackerAnnotations", self.html)
        self.assertIn("annotation-draft", self.html)
        self.assertIn("주석 상세 내용", self.html)
        self.assertIn("function undoAnnotationAction", self.html)
        self.assertIn("event.key==='Delete'", self.html)
        self.assertIn("event.key.toLowerCase()==='z'", self.html)
        self.assertIn("function cancelAnnotationMode", self.html)
        self.assertIn("graphCanvasController?.abort()", self.html)
        self.assertIn("new AbortController()", self.html)

    def test_overview_uses_admonition_icons(self) -> None:
        self.assertIn("function feedAdmonitionIcon", self.html)
        for tone in ("note", "tip", "important", "warning", "caution"):
            self.assertIn(f"feed-icon-{tone}", self.html)

    def test_overview_feed_has_readable_vertical_spacing(self) -> None:
        self.assertIn(".project-feed{gap:18px}", self.html)
        self.assertIn(".feed-list-row{padding:10px 0}", self.html)

    def test_portfolio_is_flat_project_feed(self) -> None:
        portfolio = render_portfolio([{
            "href": "projects/demo/index.html",
            "created_at": "2026-07-15T00:00:00+00:00",
            "name": "Demo",
            "milestone": "MVP",
            "summary": {"commits": 2, "changes": 1, "risks": 0},
            "risks": [],
            "top_domains": ["Core"],
        }])
        self.assertIn("Latest scans", portfolio)
        self.assertIn("1 tracked", portfolio)
        self.assertNotIn("프로젝트의 흐름을", portfolio)
        self.assertNotIn("gradient(", portfolio)


if __name__ == "__main__":
    unittest.main()
