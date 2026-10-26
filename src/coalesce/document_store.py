"""Atomic persistence for one coalesce document."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .assembly import Assembly, assembly_from_dict, assembly_to_dict
from .graph import Graph, GraphError
from .serialization import dumps, loads

MAX_RECEIPTS = 100
MAX_REQUEST_HASHES = 10_000


def _assembly_for_payload(payload: dict[str, Any]) -> Assembly:
    version = payload.get("formatVersion")
    if type(version) is int and version == 1:
        return Assembly()
    if type(version) is int and version in {2, 3}:
        if payload.get("assembly") is None:
            raise GraphError(
                "INVALID_DOCUMENT",
                f"document format version {version} requires assembly",
                field="assembly",
            )
        return assembly_from_dict(payload["assembly"], require_mates=version == 3)
    raise GraphError(
        "INVALID_DOCUMENT",
        f"unsupported document format version {version}; a newer coalesce wrote it",
        formatVersion=version,
    )


@dataclass
class Document:
    graph: Graph
    assembly: Assembly = field(default_factory=Assembly)
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
        assembly = _assembly_for_payload(payload)
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
            assembly=assembly,
            revision=int(payload.get("revision", 0)),
            receipts=receipts,
            request_hashes=list(request_hashes)[-MAX_REQUEST_HASHES:],
        )

    def save(self, document: Document) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "formatVersion": 3,
            "units": "mm",
            "revision": document.revision,
            "graph": json.loads(dumps(document.graph)),
            "assembly": assembly_to_dict(document.assembly),
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
