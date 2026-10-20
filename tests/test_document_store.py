import json

import pytest

from coalesce.document_store import Document, DocumentStore
from coalesce.graph import Cut, Extrude, Graph, GraphError, Rectangle, Sketch


def test_persisted_fork_loads_then_reports_a_clear_validation_error(tmp_path):
    path = tmp_path / "fork.coalesce.json"
    graph = Graph()
    graph.add(Sketch("plate_sketch", entities=[Rectangle("plate", 60, 40)]))
    graph.add(Extrude("wall", sketch="plate_sketch", height=25))
    graph.add(Sketch("first_tool", entities=[Rectangle("first", 4, 4)]))
    graph.add(Cut("holes", target="wall", tool="first_tool"))
    graph.add(Sketch("second_tool", entities=[Rectangle("second", 4, 4)]))
    graph.add(Cut("portcut", target="wall", tool="second_tool"))
    DocumentStore(path).save(Document(graph, revision=6))

    loaded = DocumentStore(path).load()

    with pytest.raises(GraphError) as caught:
        loaded.graph.validate()
    assert caught.value.error["code"] == "target_already_consumed"
    assert caught.value.error["target"] == "wall"
    assert caught.value.error["consumer"] == "holes"
    assert caught.value.error["terminalFeatureId"] == "holes"


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
