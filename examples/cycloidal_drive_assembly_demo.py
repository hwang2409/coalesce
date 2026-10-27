"""compatibility adapter for the packaged cycloidal drive demo."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from coalesce import demo as _demo
from coalesce.demo import *


def __getattr__(name):
    """keep legacy imports working while the implementation lives in coalesce.demo."""
    return getattr(_demo, name)


run_demo = _demo.run_demo


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--screenshots", action="store_true")
    arguments = parser.parse_args()
    try:
        asyncio.run(run_demo(arguments.out, screenshots=arguments.screenshots))
    except (_demo.DemoFailure, OSError, FileExistsError) as error:
        print(f"demo failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
