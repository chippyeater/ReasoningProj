import hashlib
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.prompt_loader import load_prompt


PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
RENAMED_PROMPT_HASHES = {
    "extraction_system_prompt.md": "08d483e75d35b4026987885385dcdc8e0aab94f074995fd559c045d1fd52b1f2",
    "extraction_evidence_repair_prompt.md": "da65737a1047f701515518b491323b0ff2315d2476d4739ed3a119b7ed977d04",
    "info_unit_agent_prompt.md": "e997749919adb844e0cb16fe7b3d334ea787821cc365ab41491dc12696bfd7d4",
}


class TestPromptInventory(TestCase):
    def test_prompt_cleanup_and_rename_preserve_runtime_content(self):
        expected = {
            *RENAMED_PROMPT_HASHES,
            "relation_system_prompt.md",
            "reasoning_selector_prompt.md",
            "conflict_layer_composer_prompt.md",
            "timeline_composer_prompt.md",
            "hypothesis_composer_prompt.md",
        }
        self.assertEqual({path.name for path in PROMPTS_DIR.glob("*.md")}, expected)
        for filename, expected_hash in RENAMED_PROMPT_HASHES.items():
            with self.subTest(filename=filename):
                content = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
                self.assertEqual(hashlib.sha256(content.encode("utf-8")).hexdigest(), expected_hash)


class TestPromptVersioning(TestCase):
    @staticmethod
    def git(root: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments], cwd=root, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def test_content_versions_are_independent_and_dirty_prompt_has_no_git_commit(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            extraction_path = root / "extraction.md"
            repair_path = root / "evidence-repair.md"
            extraction_path.write_text("extraction prompt\n", encoding="utf-8")
            repair_path.write_text("evidence repair prompt\n", encoding="utf-8")
            self.git(root, "init", "-q")
            self.git(root, "config", "user.name", "LogicWeaver Test")
            self.git(root, "config", "user.email", "test@logicweaver.invalid")
            self.git(root, "add", ".")
            self.git(root, "commit", "-q", "-m", "add prompts")

            extraction_before = load_prompt(
                extraction_path, "extraction", repository_root=root)
            repair_before = load_prompt(
                repair_path, "evidence_repair", repository_root=root)
            prompt_commit = extraction_before.git_commit
            self.assertIsNotNone(prompt_commit)

            (root / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
            self.git(root, "add", "unrelated.txt")
            self.git(root, "commit", "-q", "-m", "unrelated commit")
            extraction_after_commit = load_prompt(
                extraction_path, "extraction", repository_root=root)
            self.assertEqual(extraction_after_commit.version, extraction_before.version)
            self.assertEqual(extraction_after_commit.git_commit, prompt_commit)

            repair_path.write_text("changed evidence repair prompt\n", encoding="utf-8")
            extraction_after_change = load_prompt(
                extraction_path, "extraction", repository_root=root)
            repair_after_change = load_prompt(
                repair_path, "evidence_repair", repository_root=root)
            self.assertEqual(extraction_after_change.version, extraction_before.version)
            self.assertEqual(extraction_after_change.git_commit, prompt_commit)
            self.assertNotEqual(repair_after_change.version, repair_before.version)
            self.assertIsNone(repair_after_change.git_commit)
            self.assertEqual(extraction_after_change.schema_version, "0.3")
