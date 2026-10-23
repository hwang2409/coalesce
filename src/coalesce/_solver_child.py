from __future__ import annotations

import os
import sys
import threading

from .sketch_solver import _read_message, _solve_sketch_native, _write_message


def _exit_when_parent_closes(lifetime_fd: int) -> None:
    with os.fdopen(lifetime_fd, "rb") as lifetime:
        lifetime.read()
    os._exit(0)


def main() -> None:
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    if len(sys.argv) > 1:
        threading.Thread(
            target=_exit_when_parent_closes, args=(int(sys.argv[1]),), daemon=True
        ).start()
    with os.fdopen(protocol_fd, "wb") as protocol:
        while True:
            try:
                request = _read_message(sys.stdin.buffer)
            except EOFError:
                return
            try:
                response = ("result", _solve_sketch_native(request))
            except ImportError as error:
                response = ("import_error", str(error))
            except BaseException as error:  # noqa: BLE001 - report native child failures
                response = ("solver_error", repr(error))
            _write_message(protocol, response)


if __name__ == "__main__":
    main()
