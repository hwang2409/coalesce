from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from test_constrained_sketch import (
    create_request,
    fake_planegcs,
    rectangle_model,
    update_request,
)

import coalesce.server as server_module
import coalesce.sketch_solver as solver_module
from coalesce.server import TOOLS_GUIDE, create_server, run_stdio
from coalesce.service import DocumentService
from coalesce.sketch_solver import (
    MAX_FRAME_BYTES,
    SketchSolverSession,
    SolveRequest,
    SolveStatus,
    _write_message,
)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _script_child(tmp_path: Path, body: str) -> subprocess.Popen:
    script = tmp_path / "child.py"
    script.write_text(body)
    return subprocess.Popen(
        [sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )


def test_document_service_reuses_one_solver_child_for_two_edits(tmp_path, monkeypatch):
    real_start = solver_module._start_solver_child
    pids = []

    def counted_start():
        process = real_start()
        pids.append(process.pid)
        return process

    monkeypatch.setattr(solver_module, "_start_solver_child", counted_start)
    service = DocumentService(tmp_path / "doc.coalesce.json", warmup=False)
    try:
        assert service.sketch(create_request(rectangle_model()))["ok"] is True
        assert service.sketch(update_request(rectangle_model(width=32), 1, "update"))["ok"] is True
        assert len(pids) == 1
    finally:
        service.close()
    assert not _pid_exists(pids[0])


def test_concurrent_solver_calls_are_serialized(tmp_path, monkeypatch):
    active = tmp_path / "active"
    overlap = tmp_path / "overlap"
    fake = fake_planegcs(tmp_path / "fake", status="Success", delay=0.1)
    module = fake / "planegcs.py"
    text = module.read_text().replace(
        "time.sleep(0.1)",
        f"\n        marker = Path({str(active)!r})\n        if marker.exists(): Path({str(overlap)!r}).write_text('overlap')\n        marker.write_text('active')\n        time.sleep(0.1)\n        marker.unlink()",
    )
    module.write_text(text)
    monkeypatch.setenv("PYTHONPATH", str(fake))
    session = SketchSolverSession(timeout=1)
    results = []
    threads = [threading.Thread(target=lambda: results.append(session.solve(SolveRequest()))) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        session.close()
    assert [result.status for result in results] == [SolveStatus.FULLY_CONSTRAINED] * 2
    assert not overlap.exists()


@pytest.mark.parametrize("failure", ["crash", "malformed", "oversized", "timeout"])
def test_protocol_failure_kills_child_and_next_request_restarts(tmp_path, monkeypatch, failure):
    pid_file = tmp_path / "pid"
    restart_pid_file = tmp_path / "restart-pid"
    good_response = tmp_path / "response"
    with good_response.open("wb") as stream:
        _write_message(stream, ("result", solver_module.SolveResult(SolveStatus.FULLY_CONSTRAINED)))
    if failure == "crash":
        action = "os._exit(7)"
    elif failure == "malformed":
        action = "sys.stdout.buffer.write(struct.pack('!Q', 3) + b'bad'); sys.stdout.buffer.flush(); time.sleep(30)"
    elif failure == "oversized":
        action = f"sys.stdout.buffer.write(struct.pack('!Q', {MAX_FRAME_BYTES + 1})); sys.stdout.buffer.flush(); time.sleep(30)"
    else:
        action = "time.sleep(30)"
    child = _script_child(
        tmp_path,
        f"import os, struct, sys, time\nfrom pathlib import Path\nPath({str(pid_file)!r}).write_text(str(os.getpid()))\nsys.stdin.buffer.read(1)\n{action}\n",
    )
    starts = [child]

    def start():
        if starts:
            return starts.pop()
        return _script_child(
            tmp_path,
            f"import os, sys\nfrom pathlib import Path\nPath({str(restart_pid_file)!r}).write_text(str(os.getpid()))\nsys.stdin.buffer.read(1)\nsys.stdout.buffer.write(open({str(good_response)!r}, 'rb').read()); sys.stdout.buffer.flush(); sys.stdin.buffer.read()\n",
        )

    monkeypatch.setattr(solver_module, "_start_solver_child", start)
    session = SketchSolverSession(timeout=0.5)
    try:
        failed = session.solve(SolveRequest())
        pid = int(pid_file.read_text())
        assert failed.status is SolveStatus.DID_NOT_CONVERGE
        assert _wait_until(lambda: not _pid_exists(pid))
        assert session.solve(SolveRequest()).status is SolveStatus.FULLY_CONSTRAINED
    finally:
        session.close()
    assert _wait_until(lambda: not _pid_exists(int(restart_pid_file.read_text())))


@pytest.mark.parametrize("failure", ["crash", "malformed", "oversized", "timeout"])
def test_solver_failure_preserves_accepted_document(tmp_path, monkeypatch, failure):
    path = tmp_path / f"atomic-{failure}.coalesce.json"
    service = DocumentService(path, warmup=False, sketch_solve_timeout=0.5)
    assert service.sketch(create_request(rectangle_model()))["ok"] is True
    saved = path.read_bytes()
    service.close()
    service = DocumentService(path, warmup=False, sketch_solve_timeout=0.5)
    pid_file = tmp_path / "atomic-pid"
    actions = {
        "crash": "os._exit(7)",
        "malformed": "sys.stdout.buffer.write(struct.pack('!Q', 3) + b'bad'); sys.stdout.buffer.flush(); time.sleep(30)",
        "oversized": f"sys.stdout.buffer.write(struct.pack('!Q', {MAX_FRAME_BYTES + 1})); sys.stdout.buffer.flush(); time.sleep(30)",
        "timeout": "time.sleep(30)",
    }
    child = _script_child(
        tmp_path,
        f"import os, struct, sys, time\nfrom pathlib import Path\nPath({str(pid_file)!r}).write_text(str(os.getpid()))\nsys.stdin.buffer.read(1)\n{actions[failure]}\n",
    )
    monkeypatch.setattr(solver_module, "_start_solver_child", lambda: child)
    try:
        rejected = service.sketch(update_request(rectangle_model(width=42), 1, "rejected"))
    finally:
        service.close()
    assert rejected["ok"] is False
    assert rejected["revision"] == 1
    assert rejected["error"]["code"] == (
        "SKETCH_SOLVE_TIMEOUT" if failure == "timeout" else "SKETCH_SOLVER_FAILED"
    )
    assert "diagnosticGeometry" not in rejected["error"]
    assert path.read_bytes() == saved
    assert _wait_until(lambda: not _pid_exists(int(pid_file.read_text())))


def test_solve_after_close_does_not_start_another_child(tmp_path, monkeypatch):
    response = tmp_path / "response"
    with response.open("wb") as stream:
        _write_message(stream, ("result", solver_module.SolveResult(SolveStatus.FULLY_CONSTRAINED)))
    pids = []

    def start():
        child = _script_child(
            tmp_path,
            f"import sys\nsys.stdin.buffer.read(1)\nsys.stdout.buffer.write(open({str(response)!r}, 'rb').read()); sys.stdout.buffer.flush()\n",
        )
        pids.append(child.pid)
        return child

    monkeypatch.setattr(solver_module, "_start_solver_child", start)
    session = SketchSolverSession()
    session.close()

    result = session.solve(SolveRequest())

    assert result.errors[0].code == "SOLVER_FAILED"
    assert "shutting down" in result.errors[0].message
    assert pids == []


def test_close_interrupts_hung_solve_without_restarting(tmp_path, monkeypatch):
    pid_file = tmp_path / "hung-pid"
    child = _script_child(
        tmp_path,
        f"import os, sys, time\nfrom pathlib import Path\nPath({str(pid_file)!r}).write_text(str(os.getpid()))\nsys.stdin.buffer.read(1)\ntime.sleep(30)\n",
    )
    starts = []

    def start():
        starts.append(child.pid)
        return child

    monkeypatch.setattr(solver_module, "_start_solver_child", start)
    session = SketchSolverSession(timeout=120)
    result = []
    solve_thread = threading.Thread(target=lambda: result.append(session.solve(SolveRequest())))
    solve_thread.start()
    assert _wait_until(pid_file.exists)

    started = time.monotonic()
    close_thread = threading.Thread(target=session.close)
    close_thread.start()
    try:
        close_thread.join(timeout=1)
        assert not close_thread.is_alive()
        elapsed = time.monotonic() - started
        assert elapsed < 1
        solve_thread.join(timeout=1)
        assert not solve_thread.is_alive()
        assert result[0].errors[0].code == "SOLVER_FAILED"
        assert starts == [child.pid]
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        solve_thread.join(timeout=1)
        close_thread.join(timeout=1)


def test_close_before_delayed_startup_regeneration_never_starts_child(tmp_path, monkeypatch):
    class DelayedLoader:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def start(self):
            self.started.set()

        def wait(self):
            self.release.wait()

    loader = DelayedLoader()
    service = DocumentService(
        tmp_path / "delayed.coalesce.json", warmup=False, kernel_loader=loader
    )
    regeneration_done = threading.Event()
    starts = []

    def regenerate(_graph):
        try:
            return service._solver.solve(SolveRequest())
        finally:
            regeneration_done.set()

    monkeypatch.setattr(service, "_regenerate", regenerate)

    def unexpected_start():
        starts.append("started")
        raise RuntimeError("solver child started after close")

    monkeypatch.setattr(solver_module, "_start_solver_child", unexpected_start)
    service.start_kernel_warmup()
    assert loader.started.wait(1)

    service.close()
    loader.release.set()

    assert regeneration_done.wait(1)
    assert starts == []


@pytest.mark.anyio
async def test_run_stdio_close_does_not_block_event_loop(monkeypatch):
    close_finished = []
    heartbeat = []

    class Service:
        def close(self):
            time.sleep(0.3)
            close_finished.append(time.monotonic())

    class Server:
        def create_initialization_options(self):
            return None

        async def run(self, _read, _write, _options):
            return None

    @asynccontextmanager
    async def fake_stdio_server():
        yield object(), object()

    monkeypatch.setattr(server_module, "create_server", lambda _service: Server())
    monkeypatch.setattr(server_module, "stdio_server", fake_stdio_server)

    async def record_heartbeat():
        await anyio.sleep(0.05)
        heartbeat.append(time.monotonic())

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(run_stdio, Service())
        tasks.start_soon(record_heartbeat)

    assert heartbeat[0] < close_finished[0]


def test_missing_optional_dependency_reaps_child(tmp_path, monkeypatch):
    pid_file = tmp_path / "pid"
    response = tmp_path / "response"
    with response.open("wb") as stream:
        _write_message(stream, ("import_error", "install coalesce[sketch]"))
    child = _script_child(
        tmp_path,
        f"import os, sys\nfrom pathlib import Path\nPath({str(pid_file)!r}).write_text(str(os.getpid()))\nsys.stdin.buffer.read(1)\nsys.stdout.buffer.write(open({str(response)!r}, 'rb').read()); sys.stdout.buffer.flush(); sys.stdin.buffer.read()\n",
    )
    monkeypatch.setattr(solver_module, "_start_solver_child", lambda: child)
    session = SketchSolverSession()
    with pytest.raises(ImportError, match="coalesce"):
        session.solve(SolveRequest())
    assert _wait_until(lambda: not _pid_exists(int(pid_file.read_text())))


def test_configured_timeout_is_reported_in_server_info_and_tool_guide(tmp_path):
    service = DocumentService(
        tmp_path / "reported.coalesce.json", warmup=False, sketch_solve_timeout=2.5
    )
    try:
        info = create_server(service).create_initialization_options()
        assert "solve timeout is 2.5 seconds" in info.instructions
        assert "--sketch-solve-timeout SECONDS" in TOOLS_GUIDE
        assert "COALESCE_SKETCH_SOLVE_TIMEOUT" in TOOLS_GUIDE
    finally:
        service.close()


def test_invalid_solver_timeout_is_clean_cli_error(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "coalesce",
            "--document",
            str(tmp_path / "doc.coalesce.json"),
            "--no-viewer",
            "--sketch-solve-timeout",
            "0.1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "--sketch-solve-timeout" in result.stderr


def test_invalid_solver_timeout_environment_is_clean_cli_error(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "coalesce",
            "--document",
            str(tmp_path / "doc.coalesce.json"),
            "--no-viewer",
        ],
        env={**os.environ, "COALESCE_SKETCH_SOLVE_TIMEOUT": "infinite"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "--sketch-solve-timeout" in result.stderr
