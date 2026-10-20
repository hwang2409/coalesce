"""Atomic persistence for one coalesce document."""

from __future__ import annotations

import json
import os
import tempfile
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - export journals are POSIX-only.
    fcntl = None  # type: ignore[assignment]

from .graph import Graph
from .serialization import dumps, loads

MAX_RECEIPTS = 100
MAX_REQUEST_HASHES = 10_000


@dataclass
class Document:
    graph: Graph
    revision: int = 0
    receipts: list[dict[str, Any]] = field(default_factory=list)
    request_hashes: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class ExportJournalError(Exception):
    code: str
    message: str
    details: dict[str, Any]

    def __init__(self, code: str, message: str, **details: Any):
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "details", details)
        Exception.__init__(self, message)


class ExportJournal:
    """POSIX append-only durable receipts for mutation-free exports."""

    def __init__(self, document_path: str | Path):
        document = Path(document_path).expanduser().resolve()
        self.path = document.with_name(f"{document.name}.exports.jsonl")

    @staticmethod
    def _require_posix() -> None:
        if fcntl is None:
            raise ExportJournalError(
                "EXPORT_UNSUPPORTED",
                "durable export receipts are not supported on Windows",
            )

    def load(self) -> list[dict[str, Any]]:
        self._require_posix()
        try:
            stream = self.path.open("rb")
        except FileNotFoundError:
            return []
        with stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            content = stream.read()
        receipts = []
        for line_number, line in enumerate(content.splitlines(keepends=True), start=1):
            if not line.endswith(b"\n"):
                warnings.warn(
                    f"torn export journal line ignored in {self.path}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                break
            try:
                receipt = json.loads(line)
                if not isinstance(receipt, dict):
                    raise TypeError
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as error:
                raise ExportJournalError(
                    "EXPORT_JOURNAL_CORRUPT",
                    f"export journal has a malformed complete line {line_number}",
                    line=line_number,
                    path=str(self.path),
                ) from error
            receipts.append(receipt)
        return receipts

    def append(self, receipt: dict[str, Any]) -> None:
        self._require_posix()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            receipt, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode() + b"\n"
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            start = os.lseek(descriptor, 0, os.SEEK_END)
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                os.ftruncate(descriptor, start)
                os.fsync(descriptor)
                raise ExportJournalError(
                    "EXPORT_WRITE_FAILED",
                    "export receipt journal write was incomplete",
                    path=str(self.path),
                    expectedBytes=len(encoded),
                    writtenBytes=written,
                )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class DocumentStore:
    """Load and atomically replace one graph document."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def load(self) -> Document:
        if not self.path.exists():
            return Document(Graph())
        payload = json.loads(self.path.read_text())
        graph = loads(json.dumps(payload.get("graph", {"nodes": []})))
        receipts = list(payload.get("receipts", []))[-MAX_RECEIPTS:]
        request_hashes = payload.get("requestHashes")
        if request_hashes is None:
            request_hashes = [
                {"requestId": item["requestId"], "requestHash": item["requestHash"]}
                for item in receipts
            ]
        return Document(
            graph=graph,
            revision=int(payload.get("revision", 0)),
            receipts=receipts,
            request_hashes=list(request_hashes)[-MAX_REQUEST_HASHES:],
        )

    def save(self, document: Document) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "formatVersion": 1,
            "units": "mm",
            "revision": document.revision,
            "graph": json.loads(dumps(document.graph)),
            "receipts": document.receipts[-MAX_RECEIPTS:],
            "requestHashes": document.request_hashes[-MAX_REQUEST_HASHES:],
        }
        encoded = (
            json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode()
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                return False
            return True
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
