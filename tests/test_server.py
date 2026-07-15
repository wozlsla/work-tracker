from __future__ import annotations

import functools
import http.server
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from work_tracker.server import SecureReportHandler


class ServerTests(unittest.TestCase):
    def test_security_headers_and_directory_listing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "index.html").write_text("ok", encoding="utf-8")
            (root / ".env").write_text("OPENAI_API_KEY=secret", encoding="utf-8")
            (root / "source.cpp").write_text("int main() {}", encoding="utf-8")
            (root / "folder").mkdir()
            handler = functools.partial(SecureReportHandler, directory=str(root), quiet=True)
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(base + "/", timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
                    self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(base + "/folder/", timeout=3)
                self.assertEqual(denied.exception.code, 403)
                for path in ("/.env", "/source.cpp"):
                    with self.assertRaises(urllib.error.HTTPError) as blocked:
                        urllib.request.urlopen(base + path, timeout=3)
                    self.assertEqual(blocked.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_manual_ai_review_endpoint_persists_without_exposing_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "demo"
            project.mkdir(parents=True)
            commit_hash = "a" * 40
            report = {
                "project_name": "Demo",
                "project_path": str(root),
                "commits": [{"hash": commit_hash, "short_hash": "aaaaaaa", "working_tree": False, "review": {"status": "pending"}}],
            }
            for name in ("report.json", "state.json"):
                (project / name).write_text(json.dumps(report), encoding="utf-8")
            env_file = root / ".env"
            env_file.write_text("OPENAI_API_KEY=sk-never-exposed", encoding="utf-8")
            handler = functools.partial(SecureReportHandler, directory=str(root), quiet=True, env_file=str(env_file))
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            review = {"status": "ready", "source": "openai", "model": "gpt-test", "commit_fingerprint": commit_hash}
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                body = json.dumps({"project": "demo", "commit": commit_hash}).encode("utf-8")
                request = urllib.request.Request(
                    base + "/api/ai-review",
                    data=body,
                    headers={"Content-Type": "application/json", "X-WorkTracker-Request": "ai-review"},
                    method="POST",
                )
                with patch("work_tracker.server.generate_commit_review", return_value=review):
                    with urllib.request.urlopen(request, timeout=3) as response:
                        payload = json.loads(response.read())
                self.assertEqual(payload["review"]["status"], "ready")
                persisted = json.loads((project / "report.json").read_text(encoding="utf-8"))
                self.assertEqual(persisted["commits"][0]["review"]["model"], "gpt-test")
                self.assertNotIn("sk-never-exposed", json.dumps(persisted))
                denied = urllib.request.Request(
                    base + "/api/ai-review",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(denied, timeout=3)
                self.assertEqual(raised.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
