"""Offline Case A search diagnostics; no model calls or remote synchronization."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / "backend")]

from evals.case_a.retrieval.evaluate import main

if __name__ == "__main__":
    main()
