from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from work_tracker.openai_review import (
    OpenAIReviewConfig,
    OpenAIReviewError,
    generate_commit_review,
    load_openai_config,
    request_structured_review,
)


AI_PAYLOAD = {
    "title": "슬롯 선택 흐름 구현",
    "summary": "클라이언트 입력을 서버 검증과 상태 복제로 연결했습니다.",
    "change_type": "기능 구현",
    "impact": "medium",
    "confidence": "high",
    "pr_summary": ["슬롯 선택 상태를 서버 기준으로 동기화했습니다."],
    "structure_flow": ["Input 1~4", "ADefenseCharacter", "ULoadoutComponent::SelectSlot"],
    "component_changes": [{"component": "ULoadoutComponent", "changes": ["SelectedSlotIdx를 복제합니다."]}],
    "network_flow": ["Client", "Server RPC", "Replication", "OnRep"],
    "highlights": ["입력과 선택 책임을 분리했습니다."],
    "risks": ["잘못된 슬롯 인덱스 검증이 필요합니다."],
    "checks": ["서버와 클라이언트에서 선택 상태를 확인합니다."],
    "references": ["#12"],
}


class _FakeResponse:
    def __init__(self, payload: dict):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, amount: int = -1) -> bytes:
        return self.body if amount < 0 else self.body[:amount]


class OpenAIReviewTests(unittest.TestCase):
    def test_responses_request_uses_server_key_and_strict_schema(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(AI_PAYLOAD, ensure_ascii=False)}]}]}
            return _FakeResponse(response)

        commit = {"hash": "a" * 40, "subject": "feat: loadout", "body": "Refs #12", "files": []}
        config = OpenAIReviewConfig("sk-test-secret", "gpt-test", "medium", 50_000, 90)
        result = request_structured_review("Demo", commit, "diff --git a/a b/a\n+change", False, config, urlopen=fake_urlopen)
        request_body = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(result["title"], AI_PAYLOAD["title"])
        self.assertEqual(captured["timeout"], 90)
        self.assertEqual(captured["request"].get_header("Authorization"), "Bearer sk-test-secret")
        self.assertNotIn("sk-test-secret", captured["request"].data.decode("utf-8"))
        self.assertFalse(request_body["store"])
        self.assertTrue(request_body["text"]["format"]["strict"])
        self.assertEqual(request_body["text"]["format"]["type"], "json_schema")
        self.assertEqual(request_body["input"][0]["role"], "developer")

    def test_manual_review_is_persistable_and_key_is_not_returned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary)
            env_file = root / ".env"
            env_file.write_text("OPENAI_API_KEY=sk-local\nOPENAI_MODEL=gpt-test\n", encoding="utf-8")
            commit = {
                "hash": "b" * 40,
                "short_hash": "bbbbbbb",
                "subject": "feat: loadout",
                "body": "Refs #12",
                "files": [{"path": "Source/Game/LoadoutComponent.cpp", "insertions": 12, "deletions": 2, "domain": "player", "area": "Player"}],
                "semantic_changes": [{"component": "ULoadoutComponent", "symbols": ["SelectSlot"]}],
                "working_tree": False,
            }
            project = {"project_name": "Demo", "project_path": str(root)}
            with patch("work_tracker.openai_review.collect_commit_patch", return_value=("diff", False)), patch(
                "work_tracker.openai_review.request_structured_review", return_value=AI_PAYLOAD
            ):
                review = generate_commit_review(project, commit, env_file)
            self.assertEqual(review["status"], "ready")
            self.assertEqual(review["source"], "openai")
            self.assertEqual(review["model"], "gpt-test")
            self.assertEqual(review["commit_fingerprint"], commit["hash"])
            self.assertIn("LoadoutComponent", review["symbols"])
            self.assertNotIn("sk-local", json.dumps(review))

    def test_missing_api_key_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            env_file = Path(temporary) / ".env"
            env_file.write_text("OPENAI_API_KEY=\n", encoding="utf-8")
            with self.assertRaises(OpenAIReviewError) as raised:
                load_openai_config(env_file)
            self.assertEqual(raised.exception.status_code, 503)
            self.assertIn("OPENAI_API_KEY", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
