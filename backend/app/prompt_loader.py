"""Load Prompt content and derive verifiable, content-addressed metadata."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class LoadedPrompt:
    name: str
    content: str
    version: str
    sha256: str
    git_commit: str | None
    schema_version: str
    release_status: str

    def metadata(self, model: str) -> dict:
        result = {
            "prompt_name": self.name,
            "prompt_version": self.version,
            "prompt_sha256": self.sha256,
            "prompt_git_commit": self.git_commit,
            "schema_version": self.schema_version,
            "model": model,
            "prompt_release_status": self.release_status,
        }
        return result


def _run_git(repository_root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes] | None:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        return subprocess.run(
            ["git", *arguments], cwd=repository_root, capture_output=True,
            check=False, timeout=3, creationflags=flags,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def prompt_git_commit(path: Path, repository_root: Path = REPOSITORY_ROOT) -> str | None:
    """Return the last commit for an unchanged tracked Prompt, otherwise None."""
    try:
        relative = path.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        return None
    tracked = _run_git(repository_root, "ls-files", "--error-unmatch", "--", relative)
    if tracked is None or tracked.returncode != 0:
        return None
    clean = _run_git(repository_root, "diff", "--quiet", "HEAD", "--", relative)
    if clean is None or clean.returncode != 0:
        return None
    committed = _run_git(repository_root, "log", "-1", "--format=%H", "--", relative)
    if committed is None or committed.returncode != 0:
        return None
    value = committed.stdout.decode("ascii", errors="ignore").strip()
    return value if len(value) == 40 and all(character in "0123456789abcdef" for character in value) else None


def _path_is_committed(path: Path, repository_root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        return False
    tracked = _run_git(repository_root, "ls-files", "--error-unmatch", "--", relative)
    clean = _run_git(repository_root, "diff", "--quiet", "HEAD", "--", relative)
    return tracked is not None and tracked.returncode == 0 and clean is not None and clean.returncode == 0


def _registry_entry(registry_path: Path, path: Path, name: str,
                    repository_root: Path) -> tuple[dict, str]:
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Prompt version registry could not be read: {exc}") from exc
    if not isinstance(registry, dict) or not isinstance(registry.get("prompts"), list):
        raise ValueError("Prompt version registry is invalid")
    schema = registry.get("schema")
    schema_version = schema.get("version") if isinstance(schema, dict) else None
    if not isinstance(schema_version, str) or not schema_version:
        raise ValueError("Prompt version registry has no valid schema.version")
    matches = [item for item in registry["prompts"]
               if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        raise ValueError(f"Prompt name must be registered exactly once: {name}")
    entry = matches[0]
    version = entry.get("version")
    registered_hash = entry.get("sha256")
    registered_path = entry.get("path")
    if (isinstance(version, bool) or not isinstance(version, int) or version < 1
            or not isinstance(registered_hash, str) or not SHA256_PATTERN.fullmatch(registered_hash)
            or not isinstance(registered_path, str)):
        raise ValueError(f"Prompt registry entry is invalid: {name}")
    try:
        expected_path = (repository_root / registered_path).resolve()
    except OSError as exc:
        raise ValueError(f"Prompt registry path is invalid: {registered_path}") from exc
    if expected_path != path.resolve():
        raise ValueError(f"Prompt path does not match versions.json for {name}")
    return entry, schema_version


def load_prompt(path: Path, name: str, *, repository_root: Path = REPOSITORY_ROOT,
                registry_path: Path | None = None) -> LoadedPrompt:
    registry_path = registry_path or repository_root / "versions.json"
    content = path.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError("Prompt is empty")
    entry, schema_version = _registry_entry(registry_path, path, name, repository_root)
    actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if actual_hash != entry["sha256"]:
        raise ValueError(
            f"Prompt content differs from versions.json for {name}; "
            "run python scripts/sync_versions.py --sync")
    git_commit = prompt_git_commit(path, repository_root)
    release_status = (
        "committed" if git_commit is not None and _path_is_committed(registry_path, repository_root)
        else "uncommitted"
    )
    return LoadedPrompt(
        name=name,
        content=content,
        version=f"v{entry['version']}",
        sha256=actual_hash,
        git_commit=git_commit,
        schema_version=schema_version,
        release_status=release_status,
    )
