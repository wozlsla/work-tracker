from __future__ import annotations

import functools
import http.server
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
from pathlib import Path

from . import __version__
from .openai_review import OpenAIReviewError, generate_commit_review


PUBLIC_ARTIFACT_SUFFIXES = {".html", ".json", ".jsonl", ".md", ".mmd", ".csv"}


def serve_directory(
    directory: Path,
    host: str,
    port: int,
    allow_remote: bool = False,
    quiet: bool = False,
    env_file: Path | None = None,
) -> None:
    root = directory.resolve()
    if not root.is_dir():
        raise ValueError(f"서비스할 출력 폴더가 없습니다: {root}")
    if not (root / "index.html").is_file():
        raise ValueError(f"WorkTracker 산출물이 아닙니다. 먼저 scan을 실행하세요: {root}")
    if not allow_remote and not _is_loopback_host(host):
        raise ValueError("원격 바인딩은 기본적으로 차단됩니다. 꼭 필요하면 --allow-remote를 명시하세요.")
    selected = find_open_port(host, port)
    handler = functools.partial(
        SecureReportHandler,
        directory=str(root),
        quiet=quiet,
        env_file=str((env_file or Path(".env")).resolve()),
    )
    with http.server.ThreadingHTTPServer((host, selected), handler) as server:
        server.daemon_threads = True
        url = f"http://{host}:{selected}/"
        if not quiet:
            print(f"WorkTracker: {url}")
            print(f"Root       : {root}")
            print(f"OpenAI env : {(env_file or Path('.env')).resolve()}")
            print("중지하려면 Ctrl+C를 누르세요.")
        try:
            server.serve_forever(poll_interval=0.4)
        except KeyboardInterrupt:
            if not quiet:
                print("\n서버를 중지했습니다.")


def find_open_port(host: str, preferred: int) -> int:
    if not 1 <= preferred <= 65535:
        raise ValueError("포트는 1~65535 범위여야 합니다.")
    for candidate in range(preferred, min(preferred + 50, 65536)):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
            except OSError:
                continue
            return candidate
    raise ValueError(f"사용 가능한 포트를 찾지 못했습니다: {preferred}~{min(preferred + 49, 65535)}")


class SecureReportHandler(http.server.SimpleHTTPRequestHandler):
    server_version = f"WorkTracker/{__version__}"
    review_lock = threading.Lock()

    def __init__(self, *args, quiet: bool = False, env_file: str | None = None, **kwargs):
        self.quiet = quiet
        self.env_file = Path(env_file or ".env").resolve()
        super().__init__(*args, **kwargs)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/ai-review":
            self._send_json(404, {"error": "지원하지 않는 API 경로입니다."})
            return
        if not _is_loopback_client(self.client_address[0]):
            self._send_json(403, {"error": "AI 분석 API는 이 컴퓨터에서만 사용할 수 있습니다."})
            return
        if self.headers.get("X-WorkTracker-Request") != "ai-review":
            self._send_json(403, {"error": "유효한 WorkTracker 요청이 아닙니다."})
            return
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            self._send_json(415, {"error": "application/json 요청만 지원합니다."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= 32 * 1024:
            self._send_json(413, {"error": "요청 크기가 올바르지 않습니다."})
            return
        try:
            request_payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "요청 JSON 형식이 올바르지 않습니다."})
            return
        if not isinstance(request_payload, dict):
            self._send_json(400, {"error": "요청 본문은 JSON 객체여야 합니다."})
            return
        project_slug = str(request_payload.get("project", ""))
        commit_hash = str(request_payload.get("commit", ""))
        if not re.fullmatch(r"[a-z0-9가-힣-]{1,120}", project_slug) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_hash):
            self._send_json(400, {"error": "프로젝트 또는 커밋 식별자가 올바르지 않습니다."})
            return
        try:
            project_dir = (Path(self.directory).resolve() / "projects" / project_slug).resolve()
            project_dir.relative_to(Path(self.directory).resolve())
            report_path = project_dir / "report.json"
            report_payload = _read_json_object(report_path)
            commit = next(
                (item for item in report_payload.get("commits", []) if isinstance(item, dict) and item.get("hash") == commit_hash),
                None,
            )
            if commit is None:
                self._send_json(404, {"error": "보고서에서 선택한 커밋을 찾지 못했습니다. 먼저 다시 스캔하세요."})
                return
            with self.review_lock:
                review = generate_commit_review(report_payload, commit, self.env_file)
                _persist_review(report_path, project_dir / "state.json", commit_hash, review)
            self._send_json(200, {"review": review})
        except OpenAIReviewError as exc:
            self._send_json(exc.status_code, {"error": str(exc)})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._send_json(500, {"error": f"분석 결과를 저장하지 못했습니다: {str(exc)[:240]}"})

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def translate_path(self, path: str) -> str:
        candidate = Path(super().translate_path(path)).resolve()
        root = Path(self.directory).resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            return str(root / ".blocked")
        if any(part.startswith(".") for part in relative.parts):
            return str(root / ".blocked")
        if candidate.suffix and candidate.suffix.casefold() not in PUBLIC_ARTIFACT_SUFFIXES:
            return str(root / ".blocked")
        return str(candidate)

    def list_directory(self, path: str):
        self.send_error(403, "Directory listing disabled")
        return None

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        if not self.quiet:
            super().log_message(format, *args)


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_loopback_client(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.casefold() == "localhost"


def _read_json_object(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError(f"보고서 파일을 찾을 수 없거나 너무 큽니다: {path.name}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"보고서 형식이 올바르지 않습니다: {path.name}")
    return payload


def _persist_review(report_path: Path, state_path: Path, commit_hash: str, review: dict) -> None:
    updated = False
    for path in (report_path, state_path):
        payload = _read_json_object(path) if path.exists() else _read_json_object(report_path)
        matched = False
        for commit in payload.get("commits", []):
            if isinstance(commit, dict) and commit.get("hash") == commit_hash:
                commit["review"] = review
                matched = True
                updated = True
                break
        if matched:
            _atomic_write_json(path, payload)
    if not updated:
        raise ValueError("저장할 커밋을 찾지 못했습니다.")


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
