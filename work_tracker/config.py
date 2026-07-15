from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import AreaRule, OwnershipRule, ProjectContext, RepositoryConfig, TeamMember


class ConfigError(RuntimeError):
    pass


def load_project_context(config_dir: Path) -> ProjectContext:
    base = config_dir.resolve()
    members_data = _load_first(base, "team_members")
    areas_data = _load_first(base, "areas")
    ownership_data = _load_first(base, "ownership")
    schedule_data = _load_first(base, "schedule")

    members: list[TeamMember] = []
    for item in _list_of_dicts(members_data.get("members", [])):
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        aliases = _strings(item.get("git_author", item.get("aliases", [])))
        aliases.extend(_strings(item.get("email", [])))
        members.append(TeamMember(name=name, role=str(item.get("role", "Unknown")), aliases=_dedupe(aliases)))

    areas: list[AreaRule] = []
    for item in _list_of_dicts(areas_data.get("areas", [])):
        name = str(item.get("name", "")).strip()
        if name:
            areas.append(AreaRule(
                name=name,
                aliases=_strings(item.get("aliases", [])),
                members=_strings(item.get("members", [])),
                match=_strings(item.get("match", [])),
            ))

    ownership: list[OwnershipRule] = []
    for repo in _list_of_dicts(ownership_data.get("repos", [])):
        repository = str(repo.get("name", "")).strip()
        for item in _list_of_dicts(repo.get("areas", [])):
            owner = str(item.get("owner", "")).strip()
            if not owner:
                continue
            ownership.append(OwnershipRule(
                repository=repository,
                owner=owner,
                label=str(item.get("label", "Owned area")),
                aliases=_strings(item.get("owner_aliases", item.get("aliases", []))),
                paths=[_normal_path(value) for value in _strings(item.get("paths", []))],
            ))

    milestone = ""
    raw_milestone = schedule_data.get("milestone", {})
    if isinstance(raw_milestone, dict):
        name = str(raw_milestone.get("name", "")).strip()
        start = str(raw_milestone.get("start", "")).strip()
        end = str(raw_milestone.get("end", "")).strip()
        milestone = name + (f" · {start} → {end}" if start or end else "")
    elif raw_milestone:
        milestone = str(raw_milestone)

    goals = []
    for item in _list_of_dicts(schedule_data.get("goals", [])):
        goals.append({
            "id": str(item.get("id", "")),
            "title": str(item.get("title", "")),
            "owner": str(item.get("owner", "")),
        })

    plan_path = base / "project_plan.md"
    project_plan = _safe_read(plan_path, 512_000)
    return ProjectContext(members, areas, ownership, project_plan, milestone, goals)


def load_repositories(config_dir: Path) -> list[RepositoryConfig]:
    base = config_dir.resolve()
    data = _load_first(base, "repos")
    result: list[RepositoryConfig] = []
    for item in _list_of_dicts(data.get("repos", [])):
        raw_path = str(item.get("path", "")).strip()
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            path = base / path
        result.append(RepositoryConfig(
            name=str(item.get("name", path.name)).strip() or path.name,
            path=str(path.resolve()),
        ))
    return result


def parse_repository_argument(value: str) -> RepositoryConfig:
    if "=" in value:
        name, raw_path = value.split("=", 1)
        path = Path(raw_path).expanduser().resolve()
        return RepositoryConfig(name.strip() or path.name, str(path))
    path = Path(value).expanduser().resolve()
    return RepositoryConfig(path.name, str(path))


def load_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ConfigError(f"설정 파일이 2 MiB 제한을 초과했습니다: {path}")
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"설정 파일을 읽을 수 없습니다: {path} ({exc})") from exc
    if not text.strip():
        return {}
    try:
        value = json.loads(text) if path.suffix.lower() == ".json" else parse_yaml(text)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"설정 파일 형식이 잘못되었습니다: {path} ({exc})") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"설정 파일의 최상위 값은 객체여야 합니다: {path}")
    return value


def parse_yaml(text: str) -> Any:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        expanded = raw.expandtabs(2).rstrip()
        indent = len(expanded) - len(expanded.lstrip(" "))
        lines.append((indent, expanded.lstrip(" ")))
    if not lines:
        return {}

    def block(index: int, indent: int) -> tuple[Any, int]:
        is_list = lines[index][1].startswith("- ") or lines[index][1] == "-"
        container: Any = [] if is_list else {}
        while index < len(lines):
            current_indent, content = lines[index]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise ValueError(f"unexpected indentation near: {content}")
            if is_list:
                if not content.startswith("-"):
                    break
                item_text = content[1:].strip()
                if not item_text:
                    if index + 1 >= len(lines) or lines[index + 1][0] <= indent:
                        container.append(None)
                        index += 1
                    else:
                        child, index = block(index + 1, lines[index + 1][0])
                        container.append(child)
                    continue
                if _has_mapping_separator(item_text):
                    key, value_text = _split_mapping(item_text)
                    item: dict[str, Any] = {key: _scalar(value_text) if value_text else None}
                    index += 1
                    if index < len(lines) and lines[index][0] > indent:
                        child_indent = lines[index][0]
                        child, index = block(index, child_indent)
                        if isinstance(child, dict):
                            item.update(child)
                        elif item[key] is None:
                            item[key] = child
                        else:
                            raise ValueError(f"invalid list mapping for: {key}")
                    container.append(item)
                    continue
                container.append(_scalar(item_text))
                index += 1
                continue

            if content.startswith("-") or not _has_mapping_separator(content):
                break
            key, value_text = _split_mapping(content)
            index += 1
            if value_text:
                container[key] = _scalar(value_text)
            elif index < len(lines) and lines[index][0] > indent:
                container[key], index = block(index, lines[index][0])
            else:
                container[key] = None
        return container, index

    value, final_index = block(0, lines[0][0])
    if final_index != len(lines):
        raise ValueError(f"could not parse line: {lines[final_index][1]}")
    return value


def _load_first(base: Path, stem: str) -> dict[str, Any]:
    for suffix in (".json", ".yaml", ".yml"):
        path = base / f"{stem}{suffix}"
        if path.exists():
            return load_data(path)
    return {}


def _has_mapping_separator(value: str) -> bool:
    return bool(re.match(r"^[^:]+:(?:\s|$)", value))


def _split_mapping(value: str) -> tuple[str, str]:
    key, remainder = value.split(":", 1)
    return key.strip().strip("\"'"), remainder.strip()


def _scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value.startswith(("[", "{", '"')):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    lowered = value.lower()
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    if lowered in {"null", "none", "~"}:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in values if str(item).strip()]


def _normal_path(value: str) -> str:
    return value.replace("\\", "/").strip().lstrip("./")


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _safe_read(path: Path, limit: int) -> str:
    try:
        if path.exists() and path.stat().st_size <= limit:
            return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        pass
    return ""
