import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import pytest
from test_mcp_cut_hole_circle import extrude, rectangle_sketch

PHASE_TIMEOUT_SECONDS = 60
DIAGNOSTIC_TIMEOUT_SECONDS = 5


class StdioServer:
    def __init__(self, document: Path):
        self.command = [
            sys.executable,
            "-m",
            "coalesce",
            "--document",
            str(document),
            "--no-viewer",
        ]
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        )
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={
                **os.environ,
                "COALESCE_DEBUG_MODULES": "1",
                "PYTHONUNBUFFERED": "1",
            },
            start_new_session=sys.platform != "win32",
            creationflags=creationflags,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self._stdout = queue.Queue()
        self._stderr = deque(maxlen=40)
        self._stderr_lock = threading.Lock()
        self._request_id = 0
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self._stdout.put(line)
        finally:
            self._stdout.put(None)

    def _read_stderr(self):
        assert self.process.stderr is not None
        for line in self.process.stderr:
            with self._stderr_lock:
                self._stderr.append(line.rstrip())

    def _stderr_tail(self):
        with self._stderr_lock:
            return "\n".join(self._stderr) or "<empty>"

    def _process_tree(self):
        if sys.platform == "win32":
            script = (
                f"$root={self.process.pid}; $all=@(Get-CimInstance Win32_Process); "
                "$ids=@($root); do { $before=$ids.Count; $ids += @($all | "
                "Where-Object { $ids -contains $_.ParentProcessId } | "
                "Select-Object -ExpandProperty ProcessId); $ids=@($ids | Sort-Object -Unique) "
                "} while ($ids.Count -gt $before); $all | "
                "Where-Object { $ids -contains $_.ProcessId } | "
                "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
                "ConvertTo-Json -Compress"
            )
            command = ["powershell.exe", "-NoProfile", "-Command", script]
        else:
            command = ["ps", "-eo", "pid=,ppid=,stat=,args="]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=DIAGNOSTIC_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return f"<process tree unavailable: {error}>"
        if sys.platform == "win32":
            return result.stdout.strip() or f"<empty; diagnostic stderr: {result.stderr[-500:]}>"

        rows = [line.strip() for line in result.stdout.splitlines()]
        descendants = {self.process.pid}
        selected = []
        while True:
            before = len(descendants)
            for row in rows:
                fields = row.split(maxsplit=3)
                if len(fields) >= 2 and int(fields[1]) in descendants:
                    descendants.add(int(fields[0]))
            if len(descendants) == before:
                break
        for row in rows:
            fields = row.split(maxsplit=3)
            if fields and int(fields[0]) in descendants:
                selected.append(row)
        return "\n".join(selected) or "<empty>"

    def _fail_timeout(self, phase, started):
        elapsed = time.monotonic() - started
        pytest.fail(
            f"stdio portability deadline exceeded\n"
            f"child command: {self.command!r}\n"
            f"phase: {phase}\n"
            f"elapsed: {elapsed:.3f}s\n"
            f"return code: {self.process.poll()!r}\n"
            f"stderr tail:\n{self._stderr_tail()}\n"
            f"server child-process tree:\n{self._process_tree()}"
        )

    def request(self, method, params, phase):
        assert self.process.stdin is not None
        self._request_id += 1
        request_id = self._request_id
        started = time.monotonic()
        self.process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            + "\n"
        )
        self.process.stdin.flush()
        while True:
            remaining = PHASE_TIMEOUT_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                self._fail_timeout(phase, started)
            try:
                line = self._stdout.get(timeout=remaining)
            except queue.Empty:
                self._fail_timeout(phase, started)
            if line is None:
                pytest.fail(
                    f"stdio server exited during {phase}; return code={self.process.poll()!r}; "
                    f"stderr tail:\n{self._stderr_tail()}"
                )
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                pytest.fail(f"non-JSON MCP stdout during {phase}: {line[-1000:]!r}")
            if message.get("id") != request_id:
                continue
            assert "error" not in message, message
            print(f"phase completed: {phase}; elapsed: {time.monotonic() - started:.3f}s")
            return message["result"]

    def notify_initialized(self):
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            + "\n"
        )
        self.process.stdin.flush()

    def close(self):
        if self.process.poll() is not None:
            return
        assert self.process.stdin is not None
        started = time.monotonic()
        self.process.stdin.close()
        try:
            self.process.wait(timeout=PHASE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self._fail_timeout("server shutdown after stdin EOF", started)

    def kill(self):
        if self.process.poll() is not None:
            return
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                capture_output=True,
                timeout=DIAGNOSTIC_TIMEOUT_SECONDS,
                check=False,
            )
        else:
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=DIAGNOSTIC_TIMEOUT_SECONDS)


def _tool(server, name, arguments, phase):
    result = server.request(
        "tools/call", {"name": name, "arguments": arguments}, phase
    )["structuredContent"]
    assert result["ok"] is True, result
    return result


def test_windows_stdio_query_compound_and_xcaf_export_lifecycle(tmp_path):
    document = tmp_path / "portability.coalesce.json"
    compound_target = tmp_path / "compound.step"
    assembly_target = tmp_path / "assembly.step"
    server = StdioServer(document)
    try:
        server.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "portability-test", "version": "1"},
            },
            "initialize",
        )
        server.notify_initialized()
        revision = _tool(server, "query", {"select": {"kind": "revision"}}, "revision query")
        assert revision["revision"] == 0

        calls = [
            (
                "sketch",
                rectangle_sketch(
                    request_id="base-sketch",
                    revision=0,
                    sketch_id="base_sketch",
                    entity_id="base",
                    width=20,
                    height=10,
                ),
            ),
            (
                "feature",
                extrude(
                    request_id="base",
                    revision=1,
                    feature_id="base",
                    sketch_id="base_sketch",
                    profile_ids=["base"],
                    distance=2,
                ),
            ),
            (
                "sketch",
                rectangle_sketch(
                    request_id="lid-sketch",
                    revision=2,
                    sketch_id="lid_sketch",
                    entity_id="lid",
                    width=18,
                    height=8,
                    offset=5,
                ),
            ),
            (
                "feature",
                extrude(
                    request_id="lid",
                    revision=3,
                    feature_id="lid",
                    sketch_id="lid_sketch",
                    profile_ids=["lid"],
                    distance=1,
                ),
            ),
        ]
        for index, (name, arguments) in enumerate(calls, start=1):
            _tool(server, name, arguments, f"model mutation {index}")

        compound = _tool(
            server,
            "export",
            {
                "action": "publish",
                "mode": "compound",
                "expectedRevision": 4,
                "path": str(compound_target),
            },
            "compound STEP publish and verifier child",
        )
        assert compound["validation"]["reimport"]["partCount"] == 2

        _tool(
            server,
            "feature",
            {
                "action": "create",
                "kind": "assembly_occurrence",
                "expectedRevision": 4,
                "requestId": "place-base",
                "occurrenceId": "base-instance",
                "parameters": {
                    "partId": "base/base",
                    "transform": {
                        "translation": {"value": [0, 0, 0], "unit": "mm"},
                        "rotation": {
                            "axis": [0, 0, 1],
                            "angle": {"value": 0, "unit": "deg"},
                        },
                    },
                },
            },
            "assembly occurrence",
        )
        preflight = _tool(
            server,
            "export",
            {"action": "preflight", "mode": "assembly", "expectedRevision": 5},
            "XCAF assembly preflight and verifier child",
        )
        published = _tool(
            server,
            "export",
            {
                "action": "publish",
                "mode": "assembly",
                "expectedRevision": 5,
                "path": str(assembly_target),
            },
            "XCAF assembly publish and verifier child",
        )
        assert preflight["validation"]["xcaf"]["occurrenceCount"] == 1
        assert published["validation"]["xcaf"]["occurrenceCount"] == 1
        assert compound_target.read_bytes().startswith(b"ISO-10303-21;")
        assert assembly_target.read_bytes().startswith(b"ISO-10303-21;")
        server.close()
        assert server.process.returncode == 0
    finally:
        server.kill()
