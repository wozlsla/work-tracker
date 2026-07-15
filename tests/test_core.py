from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from work_tracker.analyzer import analyze_risks, build_report, compare_files
from work_tracker.cli import _git_metadata_signature
from work_tracker.config import parse_yaml
from work_tracker.local_review import backfill_existing_reviews, build_local_review
from work_tracker.models import (
    CommitRecord,
    GitFileChange,
    OwnershipRule,
    ProjectContext,
    ScanLimits,
    SemanticChange,
)
from work_tracker.reporter import render_dashboard
from work_tracker.scanner import ScanInventory, scan_project
from work_tracker.semantic_diff import analyze_patch
from work_tracker.server import _is_loopback_host


class ConfigTests(unittest.TestCase):
    def test_yaml_supports_nested_lists_and_maps(self) -> None:
        parsed = parse_yaml('''
repos:
  - name: "WarZ"
    path: "../WarZ"
members:
  - name: Jimin
    aliases: ["jimin", "jiminseo0307"]
''')
        self.assertEqual(parsed["repos"][0]["name"], "WarZ")
        self.assertEqual(parsed["members"][0]["aliases"], ["jimin", "jiminseo0307"])


class ScannerTests(unittest.TestCase):
    def test_unreal_semantics_and_sensitive_file_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Source" / "Game"
            source.mkdir(parents=True)
            (root / ".env").write_text("TOKEN=secret", encoding="utf-8")
            (root / ".env.production").write_text("TOKEN=secret", encoding="utf-8")
            (source / "Game.Build.cs").write_text('PublicDependencyModuleNames.AddRange(new[]{"Core","Engine"});', encoding="utf-8")
            (source / "Hero.h").write_text('''
UCLASS()
class GAME_API AHero : public ACharacter, public IDamageable
{
  UPROPERTY() TObjectPtr<UHealthComponent> Health;
  UFUNCTION(BlueprintCallable) void Hit();
};
''', encoding="utf-8")
            (source / "Hero.cpp").write_text('''
#include "Hero.h"
AHero::AHero() { Health = CreateDefaultSubobject<UHealthComponent>(TEXT("Health")); }
void AHero::Hit() { IDamageable::Execute_OnDamage(this); }
''', encoding="utf-8")
            inventory = scan_project(root, [], ScanLimits(max_files=1_000))
            hero = next(item for item in inventory.classes if item.name == "AHero")
            self.assertEqual(hero.base_class, "ACharacter")
            self.assertIn("IDamageable", hero.interfaces)
            self.assertTrue(any(item.kind == "owns-component" for item in inventory.relationships))
            self.assertFalse(any(item.path == ".env" for item in inventory.files))
            self.assertFalse(any(item.path == ".env.production" for item in inventory.files))
            self.assertEqual(inventory.skipped.get("sensitive"), 2)

    def test_snapshot_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file = root / "main.py"
            file.write_text("print(1)", encoding="utf-8")
            limits = ScanLimits(max_files=100)
            first = scan_project(root, [], limits)
            previous = {"files": [vars_for_slots(item) for item in first.files]}
            file.write_text("print(2)", encoding="utf-8")
            (root / "new.txt").write_text("new", encoding="utf-8")
            second = scan_project(root, [], limits)
            changes = compare_files(previous, second)
            self.assertEqual({item.status for item in changes}, {"added", "modified"})


class RiskAndOutputTests(unittest.TestCase):
    def test_ownership_violation_is_high_risk(self) -> None:
        commit = CommitRecord(
            hash="a" * 40, short_hash="aaaaaaa", author="Other", email="other@example.com",
            date="2026-07-15T00:00:00+00:00", subject="change", member="Other",
            files=[GitFileChange("Source/Game/Public/Hero.h")],
        )
        context = ProjectContext(ownership=[OwnershipRule("Game", "Jimin", "Player", ["jimin"], ["Source/Game/Public/"])])
        risks = analyze_risks("Game", ScanInventory(), [commit], [], context)
        self.assertTrue(any(item.severity == "high" and item.id.startswith("ownership-") for item in risks))

    def test_dashboard_escapes_script_breakout(self) -> None:
        malicious = '</script><script>alert("x")</script>'
        report = build_report(
            malicious, Path("."), Path("output"), "2026-07-01T00:00:00+00:00", "2026-07-15T00:00:00+00:00",
            ScanInventory(), [], ProjectContext(), None,
        )
        rendered = render_dashboard(report)
        self.assertNotIn(malicious, rendered)
        self.assertIn("\\u003c/script", rendered)

    def test_remote_hosts_are_not_loopback(self) -> None:
        self.assertTrue(_is_loopback_host("127.0.0.1"))
        self.assertTrue(_is_loopback_host("::1"))
        self.assertFalse(_is_loopback_host("0.0.0.0"))

    def test_commit_review_waits_for_manual_openai_analysis_and_is_restored_by_hash(self) -> None:
        commit = CommitRecord(
            hash="b" * 40, short_hash="bbbbbbb", author="Jimin", email="jimin@example.com",
            date="2026-07-15T00:00:00+00:00", subject="feat: player input implementation",
            body="선택 상태를 서버 기준으로 동기화합니다.\n\nRefs #12",
            files=[
                GitFileChange("Source/Game/Public/Hero.h", insertions=18, domain="player"),
                GitFileChange("Source/Game/Private/Hero.cpp", insertions=42, deletions=3, domain="player"),
                GitFileChange("Config/DefaultInput.ini", insertions=6, domain="player"),
            ],
        )
        report = build_report(
            "Game", Path("."), Path("output"), "2026-07-01T00:00:00+00:00", "2026-07-15T00:00:00+00:00",
            ScanInventory(), [commit], ProjectContext(), None,
        )
        review = report.commits[0].review
        self.assertEqual(report.schema_version, "2.2")
        self.assertEqual(review.status, "pending")
        self.assertEqual(review.source, "")
        self.assertEqual(review.highlights, [])
        previous = report.as_dict()
        previous_review = previous["commits"][0]["review"]
        previous_review.update({
            "status": "ready",
            "source": "openai",
            "model": "gpt-test",
            "commit_fingerprint": commit.hash,
            "summary": "실제 diff를 분석한 결과",
            "generated_by": "openai-responses:gpt-test",
        })
        restored_commit = CommitRecord(
            hash=commit.hash, short_hash=commit.short_hash, author=commit.author, email=commit.email,
            date=commit.date, subject=commit.subject, files=commit.files,
        )
        restored = build_report(
            "Game", Path("."), Path("output"), "2026-07-01T00:00:00+00:00", "2026-07-15T00:00:00+00:00",
            ScanInventory(), [restored_commit], ProjectContext(), previous,
        )
        self.assertEqual(restored.commits[0].review.status, "ready")
        self.assertEqual(restored.commits[0].review.summary, "실제 diff를 분석한 결과")
        amended = CommitRecord(
            hash="c" * 40, short_hash="ccccccc", author=commit.author, email=commit.email,
            date=commit.date, subject=commit.subject, files=commit.files,
        )
        amended_report = build_report(
            "Game", Path("."), Path("output"), "2026-07-01T00:00:00+00:00", "2026-07-15T00:00:00+00:00",
            ScanInventory(), [amended], ProjectContext(), previous,
        )
        self.assertEqual(amended_report.commits[0].review.status, "pending")
        rendered = render_dashboard(report)
        self.assertIn('"insertions":66', rendered)
        self.assertIn('"review":', rendered)

    def test_existing_baseline_is_backfilled_without_api_and_restored(self) -> None:
        commit = CommitRecord(
            hash="d" * 40, short_hash="ddddddd", author="Jimin", email="jimin@example.com",
            date="2026-07-15T00:00:00+00:00", subject="ADD: 장비 슬롯 복제 흐름 추가",
            body="Refs #12", branches=["develop"],
            files=[
                GitFileChange("Source/Game/Public/LoadoutComponent.h", status="M", insertions=18, domain="player"),
                GitFileChange("Source/Game/Private/LoadoutComponent.cpp", status="M", insertions=42, deletions=3, domain="player"),
            ],
            semantic_changes=[SemanticChange(
                component="ULoadoutComponent",
                changes=["SelectedSlotIdx를 OnRep_SelectedSlotIdx 기반 복제로 변경"],
                symbols=["SelectSlot", "OnRep_SelectedSlotIdx"],
            )],
            semantic_flow=["Client Input", "Server RPC", "SelectedSlotIdx Replication", "OnRep_SelectedSlotIdx"],
        )
        review = build_local_review(commit, "2026-07-15T01:00:00+00:00")
        self.assertEqual(review.status, "ready")
        self.assertEqual(review.source, "codex-backfill")
        self.assertEqual(review.commit_fingerprint, commit.hash)
        self.assertIn("ULoadoutComponent", [item.component for item in review.component_changes])
        self.assertIn("SelectedSlotIdx Replication", review.network_flow)
        self.assertEqual(review.references, ["#12"])
        self.assertNotIn("OPENAI", review.generated_by.upper())

        report = build_report(
            "Game", Path("."), Path("output"), "2026-07-01T00:00:00+00:00", "2026-07-15T00:00:00+00:00",
            ScanInventory(), [commit], ProjectContext(), None,
        )
        self.assertEqual(backfill_existing_reviews(report.commits, "2026-07-15T01:00:00+00:00"), 1)
        previous = report.as_dict()
        restored_commit = CommitRecord(
            hash=commit.hash, short_hash=commit.short_hash, author=commit.author, email=commit.email,
            date=commit.date, subject=commit.subject, files=commit.files,
        )
        restored = build_report(
            "Game", Path("."), Path("output"), "2026-07-01T00:00:00+00:00", "2026-07-15T00:00:00+00:00",
            ScanInventory(), [restored_commit], ProjectContext(), previous,
        )
        self.assertEqual(restored.commits[0].review.source, "codex-backfill")
        self.assertEqual(restored.commits[0].review.status, "ready")

    def test_git_metadata_signature_changes_with_branch_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            refs = root / ".git" / "refs" / "heads"
            refs.mkdir(parents=True)
            (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            branch = refs / "main"
            branch.write_text("a" * 40 + "\n", encoding="utf-8")
            first = _git_metadata_signature(root)
            branch.write_text("b" * 41 + "\n", encoding="utf-8")
            self.assertNotEqual(first, _git_metadata_signature(root))

    def test_semantic_diff_builds_pr_flow_and_component_changes(self) -> None:
        patch = '''diff --git a/Source/Game/Private/Equipment/LoadoutComponent.cpp b/Source/Game/Private/Equipment/LoadoutComponent.cpp
+++ b/Source/Game/Private/Equipment/LoadoutComponent.cpp
@@ -1,0 +1,16 @@
+void ULoadoutComponent::SelectSlot(int32 SlotIdx)
+{
+    if (!CanSelectSlot(SlotIdx)) return;
+    ServerRPC_RequestSelectSlot(SlotIdx);
+}
+void ULoadoutComponent::ServerRPC_RequestSelectSlot_Implementation(int32 SlotIdx)
+{
+    SetSelectedSlotIdx(SlotIdx);
+}
+DOREPLIFETIME(ULoadoutComponent, SelectedSlotIdx);
+OnSelectedEquipmentChanged.Broadcast(SelectedSlotIdx);
diff --git a/Source/Game/Private/Player/DefenseCharacter.cpp b/Source/Game/Private/Player/DefenseCharacter.cpp
+++ b/Source/Game/Private/Player/DefenseCharacter.cpp
@@ -1,0 +1,4 @@
+EnhancedInputComponent->BindAction(IA_LoadoutIdx, ETriggerEvent::Started, this, &ADefenseCharacter::SelectLoadoutIdx);
+LoadoutComp->SelectSlot(SlotIdx);
'''
        changes, flow = analyze_patch(patch)
        loadout = next(item for item in changes if item.component == "ULoadoutComponent")
        self.assertTrue(any("SelectedSlotIdx" in item and "Replication" in item for item in loadout.changes))
        self.assertIn("IA_LoadoutIdx", flow)
        self.assertIn("ADefenseCharacter::SelectLoadoutIdx", flow)
        self.assertIn("LoadoutComponent::SelectSlot", flow)
        self.assertIn("SelectedSlotIdx Replication", flow)


def vars_for_slots(value):
    return {name: getattr(value, name) for name in value.__dataclass_fields__}


if __name__ == "__main__":
    unittest.main()
