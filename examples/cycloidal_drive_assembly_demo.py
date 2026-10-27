"""compatibility wrapper for the packaged cycloidal drive demo."""

# retained for the source-level example contract: the demo creates a joined journal.
JOIN_OPERATION = {"operation": "join"}

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from coalesce.demo import main, run_demo  # noqa: F401

if __name__ == "__main__":
    main()
