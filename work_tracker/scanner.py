from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .models import (
    AreaRule,
    CodeClass,
    CodeMember,
    DomainRecord,
    FileRecord,
    ModuleRecord,
    Relationship,
    ScanLimits,
)


IGNORED_DIRECTORIES = {
    ".git", ".hg", ".svn", ".vs", ".idea", ".vscode", "__pycache__", "node_modules",
    "Binaries", "DerivedDataCache", "Intermediate", "Saved", "BuildData", "Debug", "Release",
    "dist", "build", "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv",
}
IGNORED_DIRECTORY_KEYS = {name.casefold() for name in IGNORED_DIRECTORIES}
IGNORED_FILES = {".DS_Store", "Thumbs.db"}
SENSITIVE_NAMES = {
    ".env", ".env.local", ".npmrc", ".pypirc", "credentials", "credentials.json", "id_rsa",
    "id_ed25519", "secrets.json", "local.settings.json", ".git-credentials", ".netrc", "auth.json",
}
TEXT_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".py", ".js", ".jsx", ".ts", ".tsx",
    ".ini", ".json", ".md", ".txt", ".toml", ".yaml", ".yml", ".uproject", ".uplugin",
    ".usf", ".ush", ".csproj",
}
GENERATED_MARKERS = ("/generated/", "/gen/", ".generated.", ".g.cs", ".min.js")

DOMAIN_META = {
    "player": ("Player", "입력, 카메라, 플레이어 상태와 조작 흐름"),
    "enemy": ("Enemy / AI", "적 AI, 이동, 인지, 상태 전이와 최적화"),
    "combat": ("Combat", "피해, 체력, 투사체와 전투 규칙"),
    "weapon": ("Weapon / Equipment", "무기, 장비, 발사와 슬롯 흐름"),
    "interaction": ("Interaction / Deployable", "상호작용, 설치물, 픽업과 월드 액션"),
    "ui": ("UI", "화면, 위젯, HUD와 사용자 피드백"),
    "level": ("Level / Flow", "맵, 게임 모드, 스폰과 진행 흐름"),
    "platform": ("Platform / Build", "빌드, 설정, 모듈과 프로젝트 기반"),
    "data": ("Data / Tools", "데이터, 자동화, 도구와 분석 코드"),
    "core": ("Core", "공통 기반과 아직 분류되지 않은 핵심 코드"),
}


@dataclass(slots=True)
class ScanInventory:
    files: list[FileRecord] = field(default_factory=list)
    modules: list[ModuleRecord] = field(default_factory=list)
    classes: list[CodeClass] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    domains: list[DomainRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)


def scan_project(
    project_root: Path,
    area_rules: list[AreaRule],
    limits: ScanLimits,
    excluded_roots: list[Path] | None = None,
) -> ScanInventory:
    root = project_root.resolve()
    if not root.is_dir():
        raise ValueError(f"프로젝트 경로가 없습니다: {root}")

    inventory = ScanInventory(skipped=defaultdict(int))
    excluded = [item.resolve() for item in (excluded_roots or [])]
    text_by_path: dict[str, str] = {}
    module_by_prefix: list[tuple[str, str]] = []

    for full_path in _walk_files(root, excluded, limits, inventory):
        try:
            relative = full_path.relative_to(root).as_posix()
            info = full_path.stat()
        except (OSError, ValueError) as exc:
            inventory.skipped["unreadable"] += 1
            _warn_once(inventory.warnings, f"파일 메타데이터를 읽지 못했습니다: {full_path.name} ({exc})")
            continue

        extension = _effective_extension(relative)
        generated = any(marker in f"/{relative.casefold()}" for marker in GENERATED_MARKERS)
        domain = classify_domain(relative, full_path.stem)
        area = classify_area(relative, area_rules)
        module = _resolve_module(relative, module_by_prefix)
        record = FileRecord(
            path=relative,
            extension=extension,
            kind=classify_kind(relative),
            area=area,
            domain=domain,
            size=info.st_size,
            modified_utc=datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
            fingerprint=_fingerprint(full_path, info.st_size, info.st_mtime_ns, limits, inventory),
            module=module,
            generated=generated,
        )
        inventory.files.append(record)

        if _is_build_file(relative):
            text = _read_text(full_path, limits, inventory)
            if text is not None:
                module_name = full_path.name[:-len(".Build.cs")]
                prefix = Path(full_path.parent).relative_to(root).as_posix()
                module_by_prefix.append((prefix, module_name))
                inventory.modules.append(ModuleRecord(
                    name=module_name,
                    path=relative,
                    kind="Plugin" if relative.casefold().startswith("plugins/") else "Game",
                    dependencies=_extract_module_dependencies(text),
                ))
                text_by_path[relative] = text
        elif extension in TEXT_EXTENSIONS and not generated:
            text = _read_text(full_path, limits, inventory)
            if text is not None:
                text_by_path[relative] = text

    module_by_prefix.sort(key=lambda item: len(item[0]), reverse=True)
    if module_by_prefix:
        for file in inventory.files:
            file.module = _resolve_module(file.path, module_by_prefix)

    _read_descriptor_modules(root, inventory, text_by_path)
    inventory.modules = _unique_modules(inventory.modules)
    inventory.classes, inventory.relationships = _analyze_code(text_by_path, limits, inventory.warnings)
    inventory.domains = _build_domains(inventory.files, inventory.classes)
    inventory.files.sort(key=lambda item: item.path.casefold())
    inventory.classes.sort(key=lambda item: (domain_sort(item.domain), item.name.casefold()))
    inventory.relationships = _dedupe_relationships(inventory.relationships)[:limits.max_relationships]
    inventory.skipped = dict(inventory.skipped)
    return inventory


def _walk_files(root: Path, excluded: list[Path], limits: ScanLimits, inventory: ScanInventory):
    stack = [root]
    yielded = 0
    while stack:
        directory = stack.pop()
        if _is_excluded(directory, excluded):
            inventory.skipped["output"] += 1
            continue
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            inventory.skipped["unreadable"] += 1
            _warn_once(inventory.warnings, f"폴더를 읽지 못해 건너뜁니다: {directory.name} ({exc})")
            continue
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
                is_reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
                if entry.is_symlink() or is_reparse:
                    inventory.skipped["link"] += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.casefold() in IGNORED_DIRECTORY_KEYS:
                        inventory.skipped["ignored_directory"] += 1
                    else:
                        stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    inventory.skipped["special"] += 1
                    continue
                lowered_name = entry.name.casefold()
                if entry.name in IGNORED_FILES or lowered_name.startswith(".env") or lowered_name in SENSITIVE_NAMES or lowered_name.endswith((".pem", ".key", ".pfx", ".p12")):
                    inventory.skipped["sensitive"] += 1
                    continue
                if entry.name.casefold().endswith((".pdb", ".obj", ".dll", ".exe", ".pyc", ".suo", ".user")):
                    inventory.skipped["generated_binary"] += 1
                    continue
            except OSError:
                inventory.skipped["unreadable"] += 1
                continue
            yielded += 1
            if yielded > limits.max_files:
                inventory.warnings.append(f"파일 수 제한({limits.max_files:,})에 도달해 나머지를 건너뛰었습니다.")
                return
            yield Path(entry.path)


def _is_excluded(path: Path, excluded: list[Path]) -> bool:
    resolved = path.resolve()
    for root in excluded:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _fingerprint(path: Path, size: int, mtime_ns: int, limits: ScanLimits, inventory: ScanInventory) -> str:
    if size > limits.max_hash_bytes:
        inventory.skipped["large_hash"] += 1
        return f"metadata:{size}:{mtime_ns}"
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        inventory.skipped["unreadable"] += 1
        return f"unreadable:{size}:{mtime_ns}"
    return "sha256:" + digest.hexdigest()


def _read_text(path: Path, limits: ScanLimits, inventory: ScanInventory) -> str | None:
    try:
        size = path.stat().st_size
        if size > limits.max_text_bytes:
            inventory.skipped["large_text"] += 1
            return None
        raw = path.read_bytes()
        if b"\x00" in raw[:4096]:
            inventory.skipped["binary_text"] += 1
            return None
        return raw.decode("utf-8-sig", errors="replace")
    except OSError:
        inventory.skipped["unreadable"] += 1
        return None


def _read_descriptor_modules(root: Path, inventory: ScanInventory, text_by_path: dict[str, str]) -> None:
    existing = {item.name.casefold() for item in inventory.modules}
    for relative, text in text_by_path.items():
        if not relative.casefold().endswith((".uproject", ".uplugin")):
            continue
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            _warn_once(inventory.warnings, f"디스크립터 JSON을 해석하지 못했습니다: {relative}")
            continue
        for item in payload.get("Modules", []) if isinstance(payload, dict) else []:
            name = str(item.get("Name", "")).strip() if isinstance(item, dict) else ""
            if name and name.casefold() not in existing:
                existing.add(name.casefold())
                inventory.modules.append(ModuleRecord(
                    name=name,
                    path=relative,
                    kind="Plugin" if relative.casefold().endswith(".uplugin") else "Project",
                ))


def _analyze_code(text_by_path: dict[str, str], limits: ScanLimits, warnings: list[str]) -> tuple[list[CodeClass], list[Relationship]]:
    classes: list[CodeClass] = []
    relationships: list[Relationship] = []
    by_stem: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for path, text in text_by_path.items():
        by_stem[Path(path).stem.casefold()].append((path, text))
        if path.casefold().endswith((".h", ".hpp")):
            classes.extend(_extract_cpp_classes(path, text))
        elif path.casefold().endswith(".py"):
            classes.extend(_extract_simple_classes(path, text, "PythonClass", r"^class\s+([A-Za-z_]\w*)\s*(?:\(([^)]*)\))?:"))
        elif path.casefold().endswith(".cs") and not path.casefold().endswith((".build.cs", ".target.cs")):
            classes.extend(_extract_simple_classes(path, text, "CSharpClass", r"\bclass\s+([A-Za-z_]\w*)\s*(?::\s*([^\{\r\n]+))?"))

    classes = _unique_classes(classes)[:limits.max_symbols]
    class_by_name = {item.name: item for item in classes}

    for cls in classes:
        if cls.base_class:
            relationships.append(Relationship(cls.name, cls.base_class, "inherits", cls.path))
        for interface in cls.interfaces:
            relationships.append(Relationship(cls.name, interface, "implements", cls.path))

        stem = Path(cls.path).stem.casefold()
        related_texts = by_stem.get(stem, [])
        if not related_texts and stem.endswith("character"):
            related_texts = by_stem.get(stem.removesuffix("character"), [])
        for source_path, text in related_texts:
            _augment_cpp_class(cls, source_path, text, relationships)

    known = set(class_by_name)
    for path, text in text_by_path.items():
        if not path.casefold().endswith((".h", ".hpp", ".cpp", ".cc")):
            continue
        owner = _source_owner(text, Path(path).stem, known)
        if not owner:
            continue
        for include in re.findall(r"(?m)^\s*#include\s+[<\"]([^>\"]+)[>\"]", text):
            relationships.append(Relationship(owner, include, "includes", path))
        for target, method in re.findall(r"([IU][A-Za-z_]\w*)::Execute_([A-Za-z_]\w*)", text):
            relationships.append(Relationship(owner, target, "interface-call", path, method))
        for action, method in re.findall(r"BindAction\s*\(\s*([^,]+).*?&[A-Za-z_]\w*::([A-Za-z_]\w*)", text, re.DOTALL):
            relationships.append(Relationship(action.strip(), f"{owner}.{method}", "input", path))
        for delegate, method in re.findall(r"([A-Za-z_]\w*)\.(?:AddUObject|AddDynamic|BindUObject|BindDynamic)\s*\([^;]*?&[A-Za-z_]\w*::([A-Za-z_]\w*)", text):
            relationships.append(Relationship(delegate, f"{owner}.{method}", "delegate", path))

    if len(relationships) >= limits.max_relationships:
        warnings.append(f"관계 수 제한({limits.max_relationships:,})을 적용했습니다.")
    return classes, relationships[:limits.max_relationships]


def _extract_cpp_classes(path: str, text: str) -> list[CodeClass]:
    pattern = re.compile(
        r"(?P<macro>U(?:CLASS|INTERFACE)\s*(?:\([^)]*\))?\s*)?"
        r"(?<!enum )\bclass\s+(?:(?:[A-Z][A-Z0-9_]*_API)\s+)?(?P<name>[A-Za-z_]\w*)"
        r"(?:\s*:\s*(?P<bases>[^\{\r\n]+))?\s*\{",
        re.MULTILINE,
    )
    result: list[CodeClass] = []
    for match in pattern.finditer(text):
        name = match.group("name")
        if name in {"class", "struct"}:
            continue
        bases = []
        for raw in (match.group("bases") or "").split(","):
            clean = re.sub(r"\b(public|private|protected|virtual)\b", "", raw).strip()
            if clean:
                bases.append(clean)
        body_end = text.find("\n};", match.end())
        body = text[match.end():body_end if body_end >= 0 else min(len(text), match.end() + 30_000)]
        base = next((item for item in bases if not item.startswith("I")), bases[0] if bases else "")
        interfaces = [item for item in bases if item.startswith("I") and item != name]
        macro = match.group("macro") or ""
        kind = _cpp_kind(name, base, macro)
        properties = _extract_marked_members(body, "UPROPERTY", property_mode=True)
        functions = _extract_marked_members(body, "UFUNCTION", property_mode=False)
        components = [item for item in properties if _is_component_type(item.type)]
        domain = classify_domain(path, name)
        summary_parts = [kind]
        if base:
            summary_parts.append(f"{base} 기반")
        if interfaces:
            summary_parts.append(f"{', '.join(interfaces[:3])} 구현")
        result.append(CodeClass(
            name=name,
            path=path,
            domain=domain,
            kind=kind,
            base_class=base,
            interfaces=interfaces,
            components=components,
            properties=properties,
            functions=functions,
            summary=f"{name}: " + ", ".join(summary_parts),
        ))
    return result


def _extract_simple_classes(path: str, text: str, kind: str, pattern: str) -> list[CodeClass]:
    result = []
    for match in re.finditer(pattern, text, re.MULTILINE):
        name = match.group(1)
        raw_base = match.group(2) or "" if match.lastindex and match.lastindex >= 2 else ""
        base = raw_base.split(",", 1)[0].strip()
        result.append(CodeClass(
            name=name, path=path, domain=classify_domain(path, name), kind=kind, base_class=base,
            summary=f"{name}: {kind}" + (f", {base} 기반" if base else ""),
        ))
    return result


def _extract_marked_members(body: str, marker: str, property_mode: bool) -> list[CodeMember]:
    pattern = re.compile(rf"{marker}\s*(?:\((?P<meta>[^)]*)\))?\s*(?P<decl>[^;{{}}]+[;])", re.MULTILINE)
    result = []
    for match in pattern.finditer(body):
        declaration = " ".join(match.group("decl").split()).rstrip(";")
        declaration = re.sub(r"\b(?:static|virtual|inline|FORCEINLINE|const)\b", "", declaration).strip()
        if property_mode:
            parsed = re.search(r"(?P<type>[A-Za-z_:][\w:<>,\s*&]*?)\s+(?P<name>[A-Za-z_]\w*)\s*(?:=.+)?$", declaration)
        else:
            parsed = re.search(r"(?P<type>[A-Za-z_:][\w:<>,\s*&]*?)\s+(?P<name>[A-Za-z_]\w*)\s*\(", declaration)
        if not parsed:
            continue
        result.append(CodeMember(
            name=parsed.group("name"),
            type=" ".join(parsed.group("type").split()),
            category=_meta_category(match.group("meta") or ""),
            detail=declaration[:240],
        ))
    return _dedupe_members(result)


def _augment_cpp_class(cls: CodeClass, path: str, text: str, relationships: list[Relationship]) -> None:
    component_pattern = re.compile(
        r"(?P<member>[A-Za-z_]\w*)\s*=\s*(?:CreateDefaultSubobject|ObjectInitializer\.CreateDefaultSubobject)\s*<(?P<type>[^>]+)>"
    )
    for match in component_pattern.finditer(text):
        member = CodeMember(match.group("member"), match.group("type").strip(), "DefaultSubobject", "CreateDefaultSubobject")
        if all(existing.name != member.name for existing in cls.components):
            cls.components.append(member)
        relationships.append(Relationship(cls.name, member.type, "owns-component", path, member.name))


def _build_domains(files: list[FileRecord], classes: list[CodeClass]) -> list[DomainRecord]:
    result: list[DomainRecord] = []
    for key, (title, summary) in DOMAIN_META.items():
        domain_classes = [item for item in classes if item.domain == key]
        domain_files = [item.path for item in files if item.domain == key]
        if not domain_classes and not domain_files:
            continue
        components = sorted({member.type for item in domain_classes for member in item.components})
        interfaces = sorted({interface for item in domain_classes for interface in item.interfaces})
        result.append(DomainRecord(
            key=key,
            title=title,
            summary=summary,
            classes=[item.name for item in domain_classes],
            components=components[:32],
            interfaces=interfaces[:24],
            files=domain_files[:80],
        ))
    return sorted(result, key=lambda item: domain_sort(item.key))


def classify_domain(path: str, name: str = "") -> str:
    value = f"{path}/{name}".casefold()
    rules = (
        ("ui", ("/ui/", "widget", "hud", "crosshair", "menu", "viewmodel")),
        ("weapon", ("weapon", "grenade", "projectile", "ammo", "equipment", "inventory")),
        ("enemy", ("enemy", "zombie", "/ai/", "aicontroller", "statetree", "behavior", "flock")),
        ("interaction", ("interact", "deployable", "pickup", "turret", "placement", "buildpoint")),
        ("combat", ("damage", "health", "combat", "hit", "armor")),
        ("player", ("player", "character", "pawn", "camera", "input")),
        ("level", ("gamemode", "gamestate", "level", "world", "spawn", ".umap")),
        ("platform", ("config/", ".ini", ".build.cs", ".target.cs", ".uproject", ".uplugin")),
        ("data", ("tools/", "scripts/", ".py", "dataset", "report", "analy")),
    )
    for domain, needles in rules:
        if any(needle in value for needle in needles):
            return domain
    return "core"


def classify_area(path: str, rules: list[AreaRule]) -> str:
    normalized = path.casefold()
    scored: list[tuple[int, str]] = []
    for rule in rules:
        hits = [needle for needle in rule.match if needle.casefold() in normalized]
        if hits:
            scored.append((max(len(item) for item in hits), rule.name))
    if scored:
        return max(scored)[1]
    first = path.split("/", 1)[0]
    return "Project" if first == path else first


def classify_kind(path: str) -> str:
    lower = path.casefold()
    extension = _effective_extension(path)
    if extension in {".cpp", ".c", ".cc"}:
        return "Source"
    if extension in {".h", ".hpp"}:
        return "Header"
    if extension in {".build.cs", ".target.cs", ".csproj"}:
        return "Build"
    if extension == ".ini":
        return "Config"
    if extension in {".uproject", ".uplugin"}:
        return "Descriptor"
    if extension == ".umap":
        return "Map"
    if extension == ".uasset":
        return "Blueprint" if "blueprint" in lower or "/bp_" in lower else "Asset"
    if extension in {".md", ".txt"}:
        return "Documentation"
    if extension in {".py", ".js", ".ts", ".tsx", ".cs"}:
        return "Code"
    if extension in {".json", ".yaml", ".yml", ".toml"}:
        return "Data"
    return "Other"


def domain_title(key: str) -> str:
    return DOMAIN_META.get(key, (key.title(), ""))[0]


def domain_sort(key: str) -> int:
    order = list(DOMAIN_META)
    return order.index(key) if key in order else len(order)


def explain_file(file: FileRecord) -> str:
    meanings = {
        "Source": "런타임 구현 로직", "Header": "타입 또는 API 표면", "Build": "모듈 의존성과 빌드 규칙",
        "Config": "런타임·에디터 기본값", "Descriptor": "프로젝트·플러그인 로드 설정",
        "Blueprint": "Blueprint 게임플레이 또는 UI", "Map": "레벨과 월드 구성", "Asset": "Unreal 콘텐츠",
        "Documentation": "프로젝트 문서", "Code": "도구 또는 애플리케이션 코드", "Data": "구조화 데이터",
    }
    module = f" · {file.module}" if file.module else ""
    return meanings.get(file.kind, f"{file.area} 영역 파일") + module


def _effective_extension(path: str) -> str:
    lower = path.casefold()
    if lower.endswith(".build.cs"):
        return ".build.cs"
    if lower.endswith(".target.cs"):
        return ".target.cs"
    return Path(lower).suffix


def _is_build_file(path: str) -> bool:
    return path.casefold().endswith(".build.cs")


def _extract_module_dependencies(text: str) -> list[str]:
    values = re.findall(r'"([A-Za-z_]\w*)"', text)
    excluded = {"Win64", "Editor", "Game", "Client", "Server", "Shipping", "Development", "Debug", "Public", "Private"}
    return sorted({value for value in values if 1 < len(value) <= 80 and value not in excluded}, key=str.casefold)


def _resolve_module(path: str, prefixes: list[tuple[str, str]]) -> str:
    lower = path.casefold()
    for prefix, name in prefixes:
        if lower == prefix.casefold() or lower.startswith(prefix.casefold().rstrip("/") + "/"):
            return name
    parts = path.split("/")
    if len(parts) >= 2 and parts[0].casefold() == "source":
        return parts[1]
    return ""


def _cpp_kind(name: str, base: str, macro: str) -> str:
    if "UINTERFACE" in macro or name.startswith("I"):
        return "Interface"
    if name.startswith("U") or "Component" in base:
        return "UObject / Component"
    if name.startswith("A") or any(value in base for value in ("Actor", "Pawn", "Character", "Controller")):
        return "Actor"
    return "C++ Class"


def _is_component_type(value: str) -> bool:
    return "Component" in value or value.startswith("TObjectPtr<U") and "Component" in value


def _meta_category(meta: str) -> str:
    match = re.search(r"Category\s*=\s*\"([^\"]+)\"", meta)
    return match.group(1) if match else ""


def _source_owner(text: str, stem: str, known: set[str]) -> str:
    for owner in re.findall(r"\b([A-Za-z_]\w*)::[A-Za-z_]\w*\s*\(", text):
        if owner in known:
            return owner
    matches = [item for item in known if item.casefold().lstrip("aui") == stem.casefold().lstrip("aui")]
    return matches[0] if matches else ""


def _unique_modules(items: list[ModuleRecord]) -> list[ModuleRecord]:
    result: dict[str, ModuleRecord] = {}
    for item in items:
        key = item.name.casefold()
        if key not in result or len(item.dependencies) > len(result[key].dependencies):
            result[key] = item
    return sorted(result.values(), key=lambda item: item.name.casefold())


def _unique_classes(items: list[CodeClass]) -> list[CodeClass]:
    result: dict[str, CodeClass] = {}
    for item in items:
        current = result.get(item.name.casefold())
        if current is None or item.path.casefold().endswith((".h", ".hpp")):
            result[item.name.casefold()] = item
    return list(result.values())


def _dedupe_members(items: list[CodeMember]) -> list[CodeMember]:
    result: dict[str, CodeMember] = {}
    for item in items:
        result.setdefault(item.name.casefold(), item)
    return list(result.values())


def _dedupe_relationships(items: list[Relationship]) -> list[Relationship]:
    result: dict[tuple[str, str, str, str], Relationship] = {}
    for item in items:
        key = (item.source.casefold(), item.target.casefold(), item.kind, item.detail.casefold())
        result.setdefault(key, item)
    return list(result.values())


def _warn_once(warnings: list[str], value: str, limit: int = 20) -> None:
    if value not in warnings and len(warnings) < limit:
        warnings.append(value)
