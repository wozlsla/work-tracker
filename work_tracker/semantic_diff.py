from __future__ import annotations

import re
from pathlib import PurePosixPath

from .models import SemanticChange


DEFINITION_PATTERN = re.compile(
    r"(?m)^\s*(?:[\w:<>,~*&]+\s+)+(?P<class>[A-Za-z_]\w*)::(?P<function>[A-Za-z_]\w*)\s*\("
)
DECLARATION_PATTERN = re.compile(
    r"(?m)^\s*(?:virtual\s+)?(?:[\w:<>,~*&]+\s+)+(?P<function>[A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:const\s*)?(?:override\s*)?;"
)


def analyze_patch(patch: str) -> tuple[list[SemanticChange], list[str]]:
    groups: dict[str, dict[str, object]] = {}
    flow: dict[str, list[str]] = {
        "input": [], "handler": [], "component": [], "server": [],
        "validation": [], "replication": [], "onrep": [], "event": [],
    }

    for section in patch.split("diff --git ")[1:]:
        header, _, body = section.partition("\n")
        match = re.search(r" b/(.+)$", header)
        if not match:
            continue
        path = match.group(1).replace("\\", "/")
        stem = PurePosixPath(path).stem
        group = groups.setdefault(stem, {"component": stem, "changes": [], "symbols": []})
        additions = [line[1:] for line in body.splitlines() if line.startswith("+") and not line.startswith("+++")]
        if not additions:
            continue
        text = "\n".join(additions)

        for definition in DEFINITION_PATTERN.finditer(text):
            class_name = definition.group("class")
            function = definition.group("function")
            if class_name.casefold().endswith(stem.casefold()):
                group["component"] = class_name
            _append(group["symbols"], function)
            _append(group["changes"], f"{function} 구현 추가")
            if function.startswith("ServerRPC_"):
                _append(flow["server"], f"{class_name}::{function}")
            elif function.startswith("OnRep_"):
                _append(flow["onrep"], f"{class_name}::{function}")
            elif function.startswith(("Can", "Validate")):
                _append(flow["validation"], f"{class_name}::{function}")
            elif function.startswith(("Select", "SetSelected")):
                _append(flow["component"], f"{class_name}::{function}")

        if PurePosixPath(path).suffix.casefold() in {".h", ".hpp"}:
            for declaration in DECLARATION_PATTERN.finditer(text):
                function = declaration.group("function")
                if function in {"UFUNCTION", "UPROPERTY"}:
                    continue
                _append(group["symbols"], function)
                _append(group["changes"], f"{function} 선언 추가")

        for action, class_name, handler in re.findall(
            r"BindAction\(\s*([A-Za-z_]\w+)[^;]*?&([A-Za-z_]\w*)::([A-Za-z_]\w*)", text, re.DOTALL
        ):
            _append(group["changes"], f"{action} 입력을 {handler}로 연결")
            _append(flow["input"], action)
            _append(flow["handler"], f"{class_name}::{handler}")

        for class_name, property_name in re.findall(r"DOREPLIFETIME\(\s*(\w+)\s*,\s*(\w+)\s*\)", text):
            _append(group["changes"], f"{property_name}를 Replication 대상으로 등록")
            _append(flow["replication"], f"{property_name} Replication")
            group["component"] = class_name if class_name.casefold().endswith(stem.casefold()) else group["component"]

        for onrep in re.findall(r"ReplicatedUsing\s*=\s*([A-Za-z_]\w+)", text):
            property_name = onrep.removeprefix("OnRep_")
            _append(group["changes"], f"{property_name}를 {onrep} 기반 복제로 변경")
            _append(flow["replication"], f"{property_name} Replication")
            _append(flow["onrep"], onrep)

        for function in re.findall(
            r"UFUNCTION\(\s*Server[^)]*\)\s*(?:[\w\s:<>,*&]+\s+)([A-Za-z_]\w*)\s*\(", text
        ):
            _append(group["changes"], f"{function} 서버 RPC 추가")
            _append(flow["server"], function)

        for event_name in re.findall(r"([A-Za-z_]\w*)\.Broadcast\s*\(", text):
            _append(group["changes"], f"{event_name} 이벤트 브로드캐스트 추가")
            _append(flow["event"], event_name)

        if "SetIsReplicatedByDefault(true)" in text:
            _append(group["changes"], "컴포넌트 기본 Replication 활성화")

        for target, function in re.findall(
            r"\b([A-Za-z_]\w*)\s*(?:->|\.)\s*((?:Select|Set|Request|Can)[A-Za-z_]\w*)\s*\(", text
        ):
            display_target = _component_name(target)
            _append(group["changes"], f"{display_target}의 {function} 호출")
            if function.startswith(("Can", "Validate")):
                _append(flow["validation"], f"{display_target}::{function}")
            elif function.startswith(("Select", "SetSelected")):
                _append(flow["component"], f"{display_target}::{function}")

    changes = [
        SemanticChange(
            component=str(value["component"]),
            changes=list(value["changes"])[:14],
            symbols=list(value["symbols"])[:12],
        )
        for value in groups.values()
        if value["changes"]
    ]
    ordered_flow: list[str] = []
    selections = {
        "input": flow["input"][-1:],
        "handler": flow["handler"][-1:],
        "component": ([value for value in flow["component"] if "::Select" in value][-1:] or flow["component"][-1:]),
        "server": flow["server"][-1:],
        "validation": flow["validation"][-1:],
        "replication": flow["replication"][-1:],
        "onrep": flow["onrep"][-1:],
        "event": flow["event"][-1:],
    }
    for key in ("input", "handler", "component", "server", "validation", "replication", "onrep", "event"):
        for value in selections[key]:
            _append(ordered_flow, value)
    return changes[:12], ordered_flow


def _append(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _component_name(value: str) -> str:
    if value.endswith("Comp"):
        return value[:-4] + "Component"
    return value
