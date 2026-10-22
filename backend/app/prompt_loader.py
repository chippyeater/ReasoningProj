"""Load Prompt content and derive verifiable, content-addressed metadata."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess

SCHEMA_VERSION = "0.3"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class LoadedPrompt:
    name: str
    content: str
    version: str
    git_commit: str | None
    schema_version: str

    def metadata(self, model: str) -> dict[str, str | None]:
        return {
            "prompt_name": self.name,
            "prompt_version": self.version,
            "prompt_git_commit": self.git_commit,
            "schema_version": self.schema_version,
            "model": model,
        }


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


def load_prompt(path: Path, name: str, *, schema_version: str = SCHEMA_VERSION,
                repository_root: Path = REPOSITORY_ROOT) -> LoadedPrompt:
    content = path.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError("Prompt is empty")
    return LoadedPrompt(
        name=name,
        content=content,
        version=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        git_commit=prompt_git_commit(path, repository_root),
        schema_version=schema_version,
    )
