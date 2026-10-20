import json

import pytest

from coalesce.document_store import Document, DocumentStore
from coalesce.graph import Graph, Rectangle, Sketch


def test_atomic_save_failure_keeps_previous_document(tmp_path, monkeypatch):
    path = tmp_path / "part.coalesce.json"
    store = DocumentStore(path)
    first = Document(Graph(), revision=0, receipts=[])
    store.save(first)
    before = path.read_bytes()

    changed = Graph()
    changed.add(Sketch("profile", entities=[Rectangle("outline", 60, 40)]))

    def fail_replace(source, destination):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr("coalesce.document_store.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated crash"):
        store.save(Document(changed, revision=1, receipts=[]))

    assert path.read_bytes() == before
    assert json.loads(before)["revision"] == 0
    assert not list(tmp_path.glob(".*.tmp"))
