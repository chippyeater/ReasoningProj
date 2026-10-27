import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase

from support import REPO

sys.path.insert(0, str(REPO))

from scripts.sync_versions import RegistryError, synchronize
from app.prompt_loader import load_prompt, load_composed_prompt
from app.schemas import StageRun


REGISTRY_PATH = REPO / "versions.json"


def content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


class RegistryFixture:
    def __init__(self, root: Path):
        self.root = root
        self.registry_path = root / "versions.json"
        (root / "backend/prompts").mkdir(parents=True)
        (root / "evals/case_a/tests").mkdir(parents=True)
        (root / "evals/case_a/gold").mkdir(parents=True)
        self.prompts = {
            "extraction": root / "backend/prompts/extraction_system_prompt.md",
            "evidence_repair": root / "backend/prompts/extraction_evidence_repair_prompt.md",
        }
        self.prompts["extraction"].write_text("extraction prompt\n", encoding="utf-8")
        self.prompts["evidence_repair"].write_text("evidence repair prompt\n", encoding="utf-8")
        (root / "evals/case_a/tests/test_manifest.json").write_text(
            '{"gold_version":"1.8"}\n', encoding="utf-8")
        (root / "evals/case_a/gold/evaluation_rubric.json").write_text(
            '{"version":"1.8"}\n', encoding="utf-8")
        self.write_registry()

    def registry(self) -> dict:
        return {
            "compositions": [],
            "prompts": [
                {
                    "name": name,
                    "path": path.relative_to(self.root).as_posix(),
                    "version": 1,
                    "sha256": content_hash(path),
                }
                for name, path in self.prompts.items()
            ],
            "schema": {"version": "0.3"},
            "gold": {"case_a": {
                "version": "1.8",
                "manifest": "evals/case_a/tests/test_manifest.json",
                "rubric": "evals/case_a/gold/evaluation_rubric.json",
            }},
        }

    def write_registry(self, value: dict | None = None) -> None:
        self.registry_path.write_text(
            json.dumps(value or self.registry(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def read_registry(self) -> dict:
        return json.loads(self.registry_path.read_text(encoding="utf-8"))

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments], cwd=self.root, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def commit(self, message: str) -> str:
        self.git("add", ".")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def initialize_git(self) -> str:
        self.git("init", "-q")
        self.git("config", "user.name", "LogicWeaver Test")
        self.git("config", "user.email", "test@logicweaver.invalid")
        return self.commit("initial registry")


class TestPromptRegistry(TestCase):
    def test_compositions_track_sources_order_hash_and_independent_releases(self):
        with TemporaryDirectory() as temporary:
            fixture = RegistryFixture(Path(temporary))
            registry = fixture.read_registry()
            registry["compositions"] = [{"name": "combined", "components": ["extraction", "evidence_repair"]}]
            fixture.write_registry(registry)
            commit = fixture.initialize_git()
            def load():
                return load_composed_prompt("combined", repository_root=fixture.root)
            before = load()
            self.assertEqual(before.content, "extraction prompt\n\n\nevidence repair prompt\n")
            self.assertEqual(before.sha256, hashlib.sha256(before.content.encode()).hexdigest())
            self.assertEqual(before, load())
            self.assertEqual(before.git_commit, commit)
            self.assertEqual(before.release_status, "committed")
            self.assertEqual([p["prompt_version"] for p in before.metadata("test")["prompt_components"]], ["v1", "v1"])
            fixture.prompts["evidence_repair"].write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "--sync"):
                load()
            self.assertEqual(synchronize(fixture.root, fixture.registry_path, write=True), ["evidence_repair"])
            changed = load()
            self.assertNotEqual(changed.version, before.version)
            self.assertEqual([p.version for p in changed.components], ["v1", "v2"])
            self.assertIsNone(changed.git_commit)
            registry = fixture.read_registry()
            registry["compositions"][0]["components"].reverse()
            fixture.write_registry(registry)
            self.assertNotEqual(load().sha256, changed.sha256)
            self.assertEqual(synchronize(fixture.root, fixture.registry_path, write=False), [])

    def test_invalid_composition_registry_is_rejected_atomically(self):
        for names in (["extraction", "missing"], ["extraction", "extraction"], ["extraction"]):
            with self.subTest(names=names), TemporaryDirectory() as temporary:
                fixture = RegistryFixture(Path(temporary))
                registry = fixture.read_registry()
                registry["compositions"] = [{"name": "combined", "components": names}]
                fixture.write_registry(registry)
                before = fixture.registry_path.read_bytes()
                with self.assertRaises(RegistryError):
                    synchronize(fixture.root, fixture.registry_path, write=True)
                self.assertEqual(fixture.registry_path.read_bytes(), before)

    def test_registered_repository_prompts_load_and_match_registry(self):
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        registered_paths = {entry["path"] for entry in registry["prompts"]}
        actual_paths = {
            path.relative_to(REPO).as_posix()
            for path in (REPO / "backend/prompts").glob("*.md")
        }
        self.assertEqual(registered_paths, actual_paths)
        for entry in registry["prompts"]:
            with self.subTest(name=entry["name"]):
                path = REPO / entry["path"]
                loaded = load_prompt(path, entry["name"])
                self.assertEqual(loaded.version, f"v{entry['version']}")
                self.assertEqual(loaded.sha256, entry["sha256"])
                self.assertEqual(loaded.sha256, content_hash(path))
                self.assertEqual(loaded.schema_version, "0.3")

    def test_check_sync_increment_and_repeat_are_prompt_local(self):
        with TemporaryDirectory() as temporary:
            fixture = RegistryFixture(Path(temporary))
            before = fixture.read_registry()
            extraction = fixture.prompts["extraction"]
            extraction.write_text("changed extraction prompt\n", encoding="utf-8")
            with self.assertRaisesRegex(RegistryError, "--sync"):
                synchronize(fixture.root, fixture.registry_path, write=False)
            self.assertEqual(synchronize(fixture.root, fixture.registry_path, write=True), ["extraction"])
            after = fixture.read_registry()
            before_by_name = {item["name"]: item for item in before["prompts"]}
            after_by_name = {item["name"]: item for item in after["prompts"]}
            self.assertEqual(after_by_name["extraction"]["version"], 2)
            self.assertEqual(after_by_name["extraction"]["sha256"], content_hash(extraction))
            self.assertEqual(after_by_name["evidence_repair"], before_by_name["evidence_repair"])
            self.assertEqual(after["schema"], before["schema"])
            self.assertEqual(after["gold"], before["gold"])
            stable_bytes = fixture.registry_path.read_bytes()
            self.assertEqual(synchronize(fixture.root, fixture.registry_path, write=True), [])
            self.assertEqual(fixture.registry_path.read_bytes(), stable_bytes)
            self.assertEqual(synchronize(fixture.root, fixture.registry_path, write=False), [])

    def test_unregistered_and_invalid_entries_fail_without_writing(self):
        with TemporaryDirectory() as temporary:
            fixture = RegistryFixture(Path(temporary))
            (fixture.root / "backend/prompts/new_prompt.md").write_text("new\n", encoding="utf-8")
            stable = fixture.registry_path.read_bytes()
            with self.assertRaisesRegex(RegistryError, "Unregistered Prompt"):
                synchronize(fixture.root, fixture.registry_path, write=True)
            self.assertEqual(fixture.registry_path.read_bytes(), stable)

        for mutation, message in (
            (lambda value: value["prompts"].__setitem__(1, {**value["prompts"][1], "name": "extraction"}),
             "Duplicate Prompt name"),
            (lambda value: value["prompts"][0].__setitem__("version", 0), "positive integer"),
            (lambda value: value["prompts"][0].__setitem__("path", "backend/prompts/missing.md"),
             "Cannot read Prompt"),
        ):
            with self.subTest(message=message), TemporaryDirectory() as temporary:
                fixture = RegistryFixture(Path(temporary))
                registry = fixture.read_registry()
                mutation(registry)
                fixture.write_registry(registry)
                stable = fixture.registry_path.read_bytes()
                with self.assertRaisesRegex(RegistryError, message):
                    synchronize(fixture.root, fixture.registry_path, write=True)
                self.assertEqual(fixture.registry_path.read_bytes(), stable)

    def test_loader_rejects_unsynced_content_and_marks_synced_uncommitted_release(self):
        with TemporaryDirectory() as temporary:
            fixture = RegistryFixture(Path(temporary))
            initial_commit = fixture.initialize_git()
            extraction = fixture.prompts["extraction"]
            loaded = load_prompt(
                extraction, "extraction", repository_root=fixture.root,
                registry_path=fixture.registry_path)
            self.assertEqual(loaded.release_status, "committed")
            self.assertEqual(loaded.git_commit, initial_commit)
            self.assertEqual(loaded.version, "v1")

            extraction.write_text("changed extraction prompt\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "--sync"):
                load_prompt(
                    extraction, "extraction", repository_root=fixture.root,
                    registry_path=fixture.registry_path)
            synchronize(fixture.root, fixture.registry_path, write=True)
            uncommitted = load_prompt(
                extraction, "extraction", repository_root=fixture.root,
                registry_path=fixture.registry_path)
            self.assertEqual(uncommitted.version, "v2")
            self.assertEqual(uncommitted.sha256, content_hash(extraction))
            self.assertIsNone(uncommitted.git_commit)
            self.assertEqual(uncommitted.release_status, "uncommitted")

            release_commit = fixture.commit("release prompt v2")
            released = load_prompt(
                extraction, "extraction", repository_root=fixture.root,
                registry_path=fixture.registry_path)
            self.assertEqual(released.release_status, "committed")
            self.assertEqual(released.git_commit, release_commit)

    def test_historical_sha_prompt_version_remains_valid_stage_run_data(self):
        historical_hash = "a" * 64
        run = StageRun.model_validate({
            "id": "run-history", "stage": "extraction", "status": "success",
            "model": "historical-model", "prompt_version": historical_hash,
            "input_snapshot_path": "runs/history.input.json",
            "output_snapshot_path": "runs/history.output.json", "error_message": None,
            "created_at": "2026-10-20T00:00:00Z",
        })
        self.assertEqual(run.prompt_version, historical_hash)
