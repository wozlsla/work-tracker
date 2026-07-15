# WorkTracker architecture

WorkTracker is a local-first analysis pipeline. Collection, inference, storage, rendering, and serving are separated so a UI change cannot silently change the report contract.

```text
config.py ───────────────┐
scanner.py ──────────────┼─> analyzer.py ─> ProjectReport
git_tracker.py ──────────┘                      │
                                                ├─ reporter.py ─> standalone artifacts
                                                └─ server.py ──> local dashboard + manual AI review
```

## Source of truth

`report.json` is the normalized source of truth for a run. `state.json` is the next comparison baseline. HTML, Markdown, CSV, Mermaid, snapshots, and the portfolio are derived views.

## Module boundaries

- `config.py`: bounded JSON/YAML configuration parsing.
- `scanner.py`: filesystem inventory and static code relationships without following links or reparse points.
- `git_tracker.py`: bounded, non-interactive Git collection using argument arrays rather than a shell.
- `semantic_diff.py`: deterministic symbols and flow hints from text diffs.
- `local_review.py`: one-time offline review backfill for an existing baseline.
- `openai_review.py`: manual, server-side OpenAI Responses API analysis for a selected commit.
- `analyzer.py`: snapshot comparison, ownership checks, risks, and the report model.
- `reporter.py`: report data shaping, escaped embedded JSON, and artifact orchestration.
- `templates/` and `assets/`: reviewable HTML/CSS/JavaScript sources inlined into standalone output at render time.
- `server.py`: loopback-first static artifact server and the manual review endpoint.
- `cli.py`: scan, watch, serve, and self-test orchestration.

## Trust boundaries

- Project files and Git output are untrusted input with count, byte, and time limits.
- Links, reparse points, credentials, `.env*`, keys, certificates, caches, and build products are excluded from scans.
- Git runs with `shell=False`, prompts disabled, and invocation-scoped `safe.directory`.
- The OpenAI key remains in the process environment or ignored `.env`; it is never written to reports or browser JavaScript.
- AI analysis only runs after a local user clicks the commit action. Binary content is not sent.
- The HTTP server binds to loopback by default, rejects directory listings, and serves only known report artifact extensions.
- Embedded report JSON escapes HTML/script boundary characters and the dashboard uses a restrictive CSP.

## Versioning

`v1.0.0` is the cleaned WorkTracker baseline: one package name, one CLI name, no generated output in version control, and no legacy compatibility modules or commands.

`v1.1.0` separates report generation from the dashboard templates and assets, adds a keyboard command palette, and hardens the architecture workbench interaction contract.

`v1.1.1` makes initial-commit-to-HEAD history the default scan scope; `--days`, `--since`, and `--max-commits` are now explicit opt-in limits.

`v1.2.0` turns the dashboard into a flatter workbench, paginates commit intelligence in 15-item slices, and restores reliable relationship-node persistence by capturing drag input at the SVG canvas boundary.

`v1.3.0` adds local-first Activity triage: merge commits are archived by default, users can archive or restore any commit, important flags persist per project, and `start.cmd` launches the server without requiring an installed console-script entry point.

`v1.4.0` adds a persistent preset layer above the composable appearance system: ten Material Theme variants and six popular palettes map to the same semantic CSS variables without duplicating component styles.
