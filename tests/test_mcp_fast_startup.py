from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import threading
import time

import pytest

from coalesce.service import DocumentService


def _message(process: subprocess.Popen[bytes], timeout: float = 3) -> dict:
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout):
            raise TimeoutError("server did not answer within the handshake budget")
        return json.loads(process.stdout.readline())


@pytest.mark.parametrize("viewer_enabled", [True, False])
def test_initialize_does_not_wait_for_kernel(tmp_path, viewer_enabled):
    document = tmp_path / "empty.coalesce.json"
    document.write_text('{"graph":{"nodes":[]},"revision":0}')
    environment = {**os.environ, "COALESCE_DEBUG_MODULES": "1"}
    command = [sys.executable, "-m", "coalesce", "--document", str(document)]
    if not viewer_enabled:
        command.append("--no-viewer")
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    try:
        started = time.monotonic()
        process.stdin.write(
            (json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                }
            ) + "\n").encode()
        )
        process.stdin.flush()
        assert _message(process)["id"] == 1
        assert time.monotonic() - started < 3

        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n')
        process.stdin.write(b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n')
        process.stdin.flush()
        assert _message(process)["id"] == 2
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert "build123d=False" in process.stderr.read().decode()


class _SlowKernelLoader:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def start(self) -> None:
        self.started.set()

    def wait(self) -> None:
        self.release.wait()


def test_revision_query_does_not_wait_for_slow_kernel_loader(tmp_path):
    loader = _SlowKernelLoader()
    service = DocumentService(
        tmp_path / "empty.coalesce.json", warmup=False, kernel_loader=loader
    )
    finished = threading.Event()
    result = {}

    def query_revision() -> None:
        result.update(service.query({"select": {"kind": "revision"}}))
        finished.set()

    thread = threading.Thread(target=query_revision)
    thread.start()
    try:
        assert finished.wait(0.2), "revision query waited for the kernel"
        assert not loader.started.is_set()
        assert result == {"ok": True, "revision": 0, "units": "mm"}
    finally:
        loader.release.set()
        thread.join(timeout=1)
