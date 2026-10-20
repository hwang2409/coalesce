from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def _message(process: subprocess.Popen[bytes]) -> dict:
    return json.loads(process.stdout.readline())


def test_initialize_does_not_wait_for_kernel(tmp_path):
    document = tmp_path / "empty.coalesce.json"
    document.write_text('{"graph":{"nodes":[]},"revision":0}')
    environment = {**os.environ, "COALESCE_DEBUG_MODULES": "1"}
    process = subprocess.Popen(
        [sys.executable, "-m", "coalesce", "--document", str(document), "--no-viewer"],
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
