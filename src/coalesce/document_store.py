"""Atomic persistence for one coalesce document."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
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
