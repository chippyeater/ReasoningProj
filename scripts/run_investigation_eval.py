"""Default: offline dry-run. See evals/case_a/investigation/README.md."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "backend"))

from evals.case_a.investigation.runner import main

if __name__ == "__main__":
    main()
