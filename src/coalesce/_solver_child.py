"""One-shot sketch solver child with parent-death detection over stdin."""

from __future__ import annotations

import os
import sys
import threading

from .sketch_solver import _read_message, _solve_sketch_native, _write_message


def _exit_when_parent_closes() -> None:
    sys.stdin.buffer.read()
    os._exit(0)


def main() -> None:
    request = _read_message(sys.stdin.buffer)
    threading.Thread(target=_exit_when_parent_closes, daemon=True).start()
    try:
        response = ("result", _solve_sketch_native(request))
    except ImportError as error:
        response = ("import_error", str(error))
    except BaseException as error:  # noqa: BLE001 - report native child failures to the parent
        response = ("solver_error", repr(error))
    _write_message(sys.stdout.buffer, response)


if __name__ == "__main__":
    main()
