import pytest

from coalesce.export import _first_mismatch


@pytest.mark.parametrize(
    ("expected", "actual", "path"),
    [
        (1.0, 1.0 + 1e-12, None),
        (1.0, 2.0, "x"),
        (1, 1.0, None),
        (True, 1.0 + 1e-12, None),
        ({"a": 1}, {"a": 1}, None),
        ({"a": 1}, {}, "x.a"),
        ({"a": 1}, {"a": 1, "b": 2}, "x.b"),
        ({"a": {"b": [1, 2]}}, {"a": {"b": [1, 3]}}, "x.a.b[1]"),
        ([1, 2], [1], "x.length"),
        ("s", "s", None),
        ("s", "t", "x"),
        ({"a": 1}, [1], "x"),
    ],
)
def test_first_mismatch_paths(expected, actual, path):
    assert _first_mismatch(expected, actual, "x") == path


def test_first_mismatch_top_level_keys_have_no_leading_dot():
    assert _first_mismatch({"a": 1}, {"a": 2}) == "a"
    assert _first_mismatch({"a": 1}, {"a": 1, "b": 0}) == "b"
