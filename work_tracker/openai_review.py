from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import __version__


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
COMMIT_HASH_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
SOURCE_SUFFIXES = {".h", ".hpp", ".cpp", ".c", ".cc", ".cs", ".py", ".js", ".ts", ".tsx", ".jsx"}


class OpenAIReviewError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class OpenAIReviewConfig:
    api_key: str
    model: str = "gpt-5.6-terra"
    reasoning_effort: str = "medium"
    max_diff_chars: int = 160_000
    timeout_seconds: int = 180


def load_openai_config(env_file: Path) -> OpenAIReviewConfig:
    values = _read_env_file(env_file)
    # Process environment wins, while the file is re-read for every button click.
    def setting(name: str, default: str = "") -> str:
        return os.environ.get(name, values.get(name, default)).strip()

    api_key = setting("OPENAI_API_KEY")
    if not api_key:
        raise OpenAIReviewError(
            f"OPENAI_API_KEY가 없습니다. {env_file} 파일에 키를 입력한 뒤 다시 시도하세요.",
            status_code=503,
        )
    if len(api_key) > 512 or any(character.isspace() for character in api_key):
        raise OpenAIReviewError("OPENAI_API_KEY 형식이 올바르지 않습니다.", status_code=503)
    model = setting("OPENAI_MODEL", "gpt-5.6-terra") or "gpt-5.6-terra"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", model):
        raise OpenAIReviewError("OPENAI_MODEL 값이 올바르지 않습니다.", status_code=503)
    effort = setting("OPENAI_REASONING_EFFORT", "medium").casefold()
    if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
        raise OpenAIReviewError("OPENAI_REASONING_EFFORT 값이 올바르지 않습니다.", status_code=503)
    max_diff_chars = _bounded_integer(setting("OPENAI_MAX_DIFF_CHARS", "160000"), 20_000, 800_000, "OPENAI_MAX_DIFF_CHARS")
    timeout = _bounded_integer(setting("OPENAI_TIMEOUT_SECONDS", "180"), 30, 600, "OPENAI_TIMEOUT_SECONDS")
    return OpenAIReviewConfig(api_key, model, effort, max_diff_chars, timeout)


def generate_commit_review(
    project: dict[str, Any],
    commit: dict[str, Any],
    env_file: Path,
    *,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    if commit.get("working_tree"):
        raise OpenAIReviewError("작업 트리는 분석할 수 없습니다. amend까지 마친 커밋을 선택하세요.", status_code=409)
    commit_hash = str(commit.get("hash", ""))
    if not COMMIT_HASH_PATTERN.fullmatch(commit_hash):
        raise OpenAIReviewError("유효한 커밋 해시가 아닙니다.", status_code=400)
    project_root = Path(str(project.get("project_path", ""))).resolve()
    config = load_openai_config(env_file)
    patch, truncated = collect_commit_patch(project_root, commit_hash, config.max_diff_chars)
    if not patch.strip():
        raise OpenAIReviewError("이 커밋에서 분석 가능한 diff를 찾지 못했습니다.", status_code=422)
    structured = request_structured_review(
        str(project.get("project_name", project_root.name)),
        commit,
        patch,
        truncated,
        config,
        urlopen=urlopen,
    )
    return _to_persisted_review(structured, commit, config, truncated)


def collect_commit_patch(project_root: Path, commit_hash: str, max_chars: int) -> tuple[str, bool]:
    if not project_root.is_dir():
        raise OpenAIReviewError("보고서의 원본 프로젝트 경로를 찾을 수 없습니다.", status_code=409)
    environment = os.environ.copy()
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8"})
    common = ["git", "-c", f"safe.directory={project_root.as_posix()}", "-C", str(project_root)]
    try:
        verify = subprocess.run(
            [*common, "cat-file", "-e", f"{commit_hash}^{{commit}}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
            shell=False,
            env=environment,
        )
        if verify.returncode != 0:
            raise OpenAIReviewError("원본 저장소에서 선택한 커밋을 찾지 못했습니다.", status_code=409)
        completed = subprocess.run(
            [
                *common, "show", "--format=", "--unified=4", "--no-renames",
                "--no-ext-diff", "--no-textconv", "--diff-merges=first-parent", commit_hash, "--",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
            shell=False,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise OpenAIReviewError("git 실행 파일을 찾지 못했습니다.", status_code=503) from exc
    except subprocess.TimeoutExpired as exc:
        raise OpenAIReviewError("커밋 diff 수집 시간이 초과되었습니다.", status_code=504) from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise OpenAIReviewError(f"커밋 diff 수집에 실패했습니다: {(detail[-1] if detail else 'git error')[:240]}", status_code=409)
    patch = completed.stdout.decode("utf-8", errors="replace")
    if len(patch) <= max_chars:
        return patch, False
    marker = "\n\n[WorkTracker: diff가 안전 한도를 초과하여 여기서 잘렸습니다.]\n"
    return patch[: max(0, max_chars - len(marker))] + marker, True


def request_structured_review(
    project_name: str,
    commit: dict[str, Any],
    patch: str,
    truncated: bool,
    config: OpenAIReviewConfig,
    *,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    request_body = {
        "model": config.model,
        "store": False,
        "reasoning": {"effort": config.reasoning_effort},
        "max_output_tokens": 6_000,
        "input": [
            {"role": "developer", "content": _review_instructions()},
            {"role": "user", "content": _review_input(project_name, commit, patch, truncated)},
        ],
        "text": {"format": {"type": "json_schema", "name": "trace_digest_commit_review", "strict": True, "schema": _review_schema()}},
    }
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"WorkTracker/{__version__}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raw = exc.read(512 * 1024)
        detail = _api_error_message(raw) or f"HTTP {exc.code}"
        code = 401 if exc.code in {401, 403} else (429 if exc.code == 429 else 502)
        raise OpenAIReviewError(f"OpenAI API 요청에 실패했습니다: {detail}", status_code=code) from exc
    except urllib.error.URLError as exc:
        raise OpenAIReviewError(f"OpenAI API에 연결하지 못했습니다: {str(exc.reason)[:240]}", status_code=502) from exc
    except TimeoutError as exc:
        raise OpenAIReviewError("OpenAI 분석 응답 시간이 초과되었습니다.", status_code=504) from exc
    if len(raw) > 4 * 1024 * 1024:
        raise OpenAIReviewError("OpenAI 응답이 안전 크기 제한을 초과했습니다.")
    try:
        response_payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpenAIReviewError("OpenAI 응답을 JSON으로 해석하지 못했습니다.") from exc
    output_text = _extract_output_text(response_payload)
    try:
        parsed = json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise OpenAIReviewError("OpenAI 구조화 응답의 형식이 올바르지 않습니다.") from exc
    if not isinstance(parsed, dict):
        raise OpenAIReviewError("OpenAI 구조화 응답이 객체가 아닙니다.")
    return parsed


def _review_instructions() -> str:
    return """당신은 한 개의 Git 커밋을 검토하는 시니어 소프트웨어 엔지니어입니다.
반드시 제공된 커밋 메시지, 파일 메타데이터와 실제 diff만 근거로 한국어 PR 요약을 작성하세요.
추측으로 클래스, 호출, 테스트 결과를 만들지 마세요. 바이너리 또는 잘린 diff는 한계를 명시하세요.
요약은 짧게, 구조 흐름은 실제 심볼을 사용한 순서로, 변경 내용은 컴포넌트별로 작성하세요.
네트워크 흐름은 RPC/권한/복제/OnRep 근거가 있을 때만 채우세요.
위험은 가능한 실패 양상, 검증은 실행 가능한 확인 항목으로 작성하세요.
references에는 커밋 메시지에 실제로 등장한 이슈/PR 참조만 넣으세요."""


def _review_input(project_name: str, commit: dict[str, Any], patch: str, truncated: bool) -> str:
    files = [
        {
            "path": item.get("path", ""),
            "status": item.get("status", ""),
            "insertions": item.get("insertions", 0),
            "deletions": item.get("deletions", 0),
            "binary": bool(item.get("binary")),
            "domain": item.get("domain", ""),
            "area": item.get("area", ""),
        }
        for item in commit.get("files", [])
        if isinstance(item, dict)
    ]
    evidence = {
        "project": project_name,
        "commit": commit.get("hash", ""),
        "subject": commit.get("subject", ""),
        "body": commit.get("body", ""),
        "branches": commit.get("branches", []),
        "parents": commit.get("parents", []),
        "files": files,
        "precomputed_semantic_changes": commit.get("semantic_changes", []),
        "precomputed_flow": commit.get("semantic_flow", []),
        "diff_truncated": truncated,
    }
    return "커밋 근거 메타데이터:\n" + json.dumps(evidence, ensure_ascii=False, indent=2) + "\n\n실제 Git diff:\n" + patch


def _review_schema() -> dict[str, Any]:
    string_array = {"type": "array", "items": {"type": "string"}}
    properties = {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "change_type": {"type": "string"},
        "impact": {"type": "string", "enum": ["low", "medium", "high"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "pr_summary": string_array,
        "structure_flow": string_array,
        "component_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"component": {"type": "string"}, "changes": string_array},
                "required": ["component", "changes"],
                "additionalProperties": False,
            },
        },
        "network_flow": string_array,
        "highlights": string_array,
        "risks": string_array,
        "checks": string_array,
        "references": string_array,
    }
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _extract_output_text(payload: dict[str, Any]) -> str:
    if payload.get("status") == "incomplete":
        reason = (payload.get("incomplete_details") or {}).get("reason", "unknown")
        raise OpenAIReviewError(f"OpenAI 분석 응답이 완료되지 않았습니다: {reason}")
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    for item in payload.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") == "refusal":
                raise OpenAIReviewError(f"OpenAI가 분석을 거절했습니다: {str(content.get('refusal', ''))[:240]}")
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return content["text"]
    raise OpenAIReviewError("OpenAI 응답에 분석 본문이 없습니다.")


def _to_persisted_review(payload: dict[str, Any], commit: dict[str, Any], config: OpenAIReviewConfig, truncated: bool) -> dict[str, Any]:
    files = [item for item in commit.get("files", []) if isinstance(item, dict)]
    domains = _ranked_text(item.get("domain", "") for item in files)
    areas = _ranked_text(item.get("area", "") for item in files)
    symbols = _commit_symbols(commit, files)
    components = []
    for item in payload.get("component_changes", [])[:16]:
        if not isinstance(item, dict):
            continue
        component = _clean_text(item.get("component", ""), 180)
        changes = _clean_list(item.get("changes", []), 10, 500)
        if component and changes:
            components.append({"component": component, "changes": changes, "symbols": []})
    evidence = [
        _evidence_line(item)
        for item in sorted(files, key=lambda value: int(value.get("insertions", 0) or 0) + int(value.get("deletions", 0) or 0), reverse=True)[:20]
        if item.get("path")
    ]
    if truncated:
        evidence.append("diff 일부가 OPENAI_MAX_DIFF_CHARS 제한으로 잘렸습니다.")
    now = datetime.now(UTC).isoformat()
    return {
        "status": "ready",
        "source": "openai",
        "model": config.model,
        "analyzed_at": now,
        "commit_fingerprint": str(commit.get("hash", "")),
        "error": "",
        "title": _clean_text(payload.get("title", ""), 240) or f"{commit.get('short_hash', '')} 구현 분석",
        "summary": _clean_text(payload.get("summary", ""), 1_500),
        "change_type": _clean_text(payload.get("change_type", "구현 변경"), 80) or "구현 변경",
        "impact": payload.get("impact") if payload.get("impact") in {"low", "medium", "high"} else "medium",
        "confidence": payload.get("confidence") if payload.get("confidence") in {"low", "medium", "high"} else "medium",
        "highlights": _clean_list(payload.get("highlights", []), 10, 600),
        "risks": _clean_list(payload.get("risks", []), 10, 600),
        "checks": _clean_list(payload.get("checks", []), 10, 600),
        "domains": domains[:8],
        "areas": areas[:8],
        "symbols": symbols[:16],
        "evidence": evidence,
        "pr_summary": _clean_list(payload.get("pr_summary", []), 6, 700),
        "structure_flow": _clean_list(payload.get("structure_flow", []), 14, 220),
        "component_changes": components,
        "network_flow": _clean_list(payload.get("network_flow", []), 14, 220),
        "references": _clean_list(payload.get("references", []), 12, 120),
        "generated_by": f"openai-responses:{config.model}",
    }


def _commit_symbols(commit: dict[str, Any], files: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for change in commit.get("semantic_changes", []):
        if isinstance(change, dict):
            result.extend([change.get("component", ""), *change.get("symbols", [])])
    for item in files:
        path = PurePosixPath(str(item.get("path", "")))
        if path.suffix.casefold() in SOURCE_SUFFIXES and path.stem.casefold() not in {"main", "index", "__init__"}:
            result.append(path.stem)
    return _dedupe_text(result)


def _evidence_line(item: dict[str, Any]) -> str:
    stats = "binary" if item.get("binary") else f"+{item.get('insertions', 0)}/-{item.get('deletions', 0)}"
    return f"{item.get('path', '')} · {stats}"


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        if path.stat().st_size > 64 * 1024:
            raise OpenAIReviewError("환경 파일이 64 KiB 제한을 초과했습니다.", status_code=503)
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise OpenAIReviewError(f"환경 파일을 읽지 못했습니다: {path}", status_code=503) from exc
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        result[name] = value
    return result


def _api_error_message(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        message = (payload.get("error") or {}).get("message", "")
        return str(message)[:400]
    except (json.JSONDecodeError, AttributeError):
        return ""


def _bounded_integer(value: str, minimum: int, maximum: int, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise OpenAIReviewError(f"{name} 값은 정수여야 합니다.", status_code=503) from exc
    if not minimum <= parsed <= maximum:
        raise OpenAIReviewError(f"{name} 값은 {minimum}~{maximum} 범위여야 합니다.", status_code=503)
    return parsed


def _clean_text(value: Any, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _clean_list(values: Any, maximum_items: int, maximum_chars: int) -> list[str]:
    if not isinstance(values, list):
        return []
    return _dedupe_text(_clean_text(value, maximum_chars) for value in values)[:maximum_items]


def _ranked_text(values) -> list[str]:
    counts: dict[str, int] = {}
    for value in values:
        clean = _clean_text(value, 120)
        if clean:
            counts[clean] = counts.get(clean, 0) + 1
    return sorted(counts, key=lambda item: (-counts[item], item.casefold()))


def _dedupe_text(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _clean_text(value, 500)
        folded = clean.casefold()
        if clean and folded not in seen:
            seen.add(folded)
            result.append(clean)
    return result
