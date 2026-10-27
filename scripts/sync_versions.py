"""Validate or atomically synchronize LogicWeaver's Prompt registry."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPOSITORY_ROOT / "versions.json"
PROMPTS_DIRECTORY = PurePosixPath("backend/prompts")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class RegistryError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise RegistryError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except OSError as exc:
        raise RegistryError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError(f"Invalid JSON in {path}: {exc}") from exc


def _content_hash(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RegistryError(f"Cannot read Prompt {path}: {exc}") from exc
    if not content.strip():
        raise RegistryError(f"Prompt is empty: {path}")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _safe_relative_path(value: object, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise RegistryError(f"{field} must be a nonempty repository-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value != path.as_posix():
        raise RegistryError(f"{field} must be a normalized repository-relative path: {value!r}")
    return path


def validate_registry(repository_root: Path, registry: object) -> tuple[list[dict], dict[str, str]]:
    if not isinstance(registry, dict) or set(registry) != {"prompts", "compositions", "schema", "gold"}:
        raise RegistryError("versions.json must contain exactly prompts, compositions, schema, and gold")
    schema = registry["schema"]
    if not isinstance(schema, dict) or set(schema) != {"version"} or not isinstance(schema["version"], str):
        raise RegistryError("schema.version must be a string")
    gold = registry["gold"]
    if not isinstance(gold, dict) or set(gold) != {"case_a"} or not isinstance(gold["case_a"], dict):
        raise RegistryError("gold.case_a must be registered")
    case_a = gold["case_a"]
    if set(case_a) != {"version", "manifest", "rubric"} or not isinstance(case_a["version"], str):
        raise RegistryError("gold.case_a requires version, manifest, and rubric")
    manifest_path = repository_root / _safe_relative_path(case_a["manifest"], "gold.case_a.manifest")
    rubric_path = repository_root / _safe_relative_path(case_a["rubric"], "gold.case_a.rubric")
    manifest = _read_json(manifest_path)
    rubric = _read_json(rubric_path)
    manifest_version = manifest.get("gold_version") if isinstance(manifest, dict) else None
    rubric_version = rubric.get("version") if isinstance(rubric, dict) else None
    if case_a["version"] != manifest_version or case_a["version"] != rubric_version:
        raise RegistryError(
            "gold.case_a.version must match the existing Manifest and Rubric versions")

    prompts = registry["prompts"]
    if not isinstance(prompts, list) or not prompts:
        raise RegistryError("prompts must be a nonempty list")
    names: set[str] = set()
    paths: set[str] = set()
    hashes: dict[str, str] = {}
    for index, item in enumerate(prompts):
        label = f"prompts[{index}]"
        if not isinstance(item, dict) or set(item) != {"name", "path", "version", "sha256"}:
            raise RegistryError(f"{label} requires exactly name, path, version, and sha256")
        name = item["name"]
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
            raise RegistryError(f"{label}.name is invalid: {name!r}")
        if name in names:
            raise RegistryError(f"Duplicate Prompt name: {name}")
        names.add(name)
        relative = _safe_relative_path(item["path"], f"{label}.path")
        if relative.parent != PROMPTS_DIRECTORY or relative.suffix != ".md":
            raise RegistryError(f"Prompt path must be a Markdown file directly under backend/prompts: {relative}")
        path_key = relative.as_posix()
        if path_key in paths:
            raise RegistryError(f"Duplicate Prompt path: {path_key}")
        paths.add(path_key)
        version = item["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise RegistryError(f"{label}.version must be a positive integer")
        sha256 = item["sha256"]
        if not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256):
            raise RegistryError(f"{label}.sha256 must be a lowercase SHA-256")
        hashes[name] = _content_hash(repository_root / relative)

    compositions = registry["compositions"]
    if not isinstance(compositions, list):
        raise RegistryError("compositions must be a list")
    composition_names = set()
    for item in compositions:
        if not isinstance(item, dict) or set(item) != {"name", "components"}:
            raise RegistryError("Compositions require exactly name and components")
        name, components = item["name"], item["components"]
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name) or name in names | composition_names:
            raise RegistryError("Composition names must be valid and unique across all Prompts")
        composition_names.add(name)
        if (not isinstance(components, list) or len(components) < 2
                or any(not isinstance(n, str) or n not in names for n in components)
                or len(set(components)) != len(components)):
            raise RegistryError("Composition components must be ordered unique registered source names")

    actual_paths = {
        path.relative_to(repository_root).as_posix()
        for path in (repository_root / PROMPTS_DIRECTORY).glob("*.md")
    }
    unregistered = sorted(actual_paths - paths)
    missing_from_directory = sorted(paths - actual_paths)
    if unregistered:
        raise RegistryError(f"Unregistered Prompt files: {', '.join(unregistered)}")
    if missing_from_directory:
        raise RegistryError(f"Registered Prompt files are missing: {', '.join(missing_from_directory)}")
    return prompts, hashes


def synchronize(repository_root: Path, registry_path: Path, *, write: bool) -> list[str]:
    registry = _read_json(registry_path)
    prompts, hashes = validate_registry(repository_root, registry)
    changed = [item["name"] for item in prompts if item["sha256"] != hashes[item["name"]]]
    if not write:
        if changed:
            raise RegistryError(
                "Prompt content differs from versions.json: " + ", ".join(changed)
                + "; run python scripts/sync_versions.py --sync")
        return []
    if not changed:
        return []

    updated = copy.deepcopy(registry)
    for item in updated["prompts"]:
        if item["name"] in changed:
            item["version"] += 1
            item["sha256"] = hashes[item["name"]]
    # Revalidate the complete candidate before the single atomic replacement.
    validate_registry(repository_root, updated)
    serialized = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="\n", dir=registry_path.parent,
                prefix=f".{registry_path.name}.", suffix=".tmp", delete=False) as temporary:
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, registry_path)
    except OSError as exc:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise RegistryError(f"Cannot atomically update {registry_path}: {exc}") from exc
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="validate without writing")
    mode.add_argument("--sync", action="store_true", help="increment changed Prompt versions atomically")
    args = parser.parse_args()
    try:
        changed = synchronize(REPOSITORY_ROOT, REGISTRY_PATH, write=args.sync)
    except RegistryError as exc:
        parser.exit(1, f"version registry error: {exc}\n")
    if changed:
        print("Updated Prompt versions: " + ", ".join(changed))
    else:
        print("Prompt versions are synchronized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
