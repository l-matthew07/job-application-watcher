import json

import pytest

from twitter_sentiment.store import ResultStore, RunState, id_max


def test_append_and_read_round_trip(tmp_path):
    store = ResultStore(tmp_path / "out.jsonl")

    assert store.append([{"id": "1"}, {"id": "2"}]) == 2
    assert store.append([{"id": "3"}]) == 1
    assert [r["id"] for r in store.read_all()] == ["1", "2", "3"]


def test_append_is_a_noop_for_empty_input(tmp_path):
    store = ResultStore(tmp_path / "out.jsonl")

    assert store.append([]) == 0
    assert not store.path.exists()


def test_append_creates_missing_directories(tmp_path):
    store = ResultStore(tmp_path / "nested" / "deep" / "out.jsonl")
    store.append([{"id": "1"}])

    assert store.read_all() == [{"id": "1"}]


def test_read_all_on_missing_file_is_empty(tmp_path):
    assert ResultStore(tmp_path / "nope.jsonl").read_all() == []


def test_read_all_skips_a_truncated_final_line(tmp_path):
    path = tmp_path / "out.jsonl"
    path.write_text('{"id": "1"}\n\n{"id": "2"\n', encoding="utf-8")

    assert ResultStore(path).read_all() == [{"id": "1"}]


def test_non_ascii_survives_the_round_trip(tmp_path):
    store = ResultStore(tmp_path / "out.jsonl")
    store.append([{"text": "Nestlé 🔥"}])

    assert store.read_all()[0]["text"] == "Nestlé 🔥"


@pytest.mark.parametrize(
    "left,right,expected",
    [("9", "10", "10"), ("10", "9", "10"), ("", "5", "5"), ("5", "", "5"),
     ("", "", ""), ("abc", "abd", "abd")],
)
def test_id_max_compares_snowflakes_numerically(left, right, expected):
    assert id_max(left, right) == expected


def test_since_id_round_trips_across_instances(tmp_path):
    path = tmp_path / "state.json"
    state = RunState(path)
    state.advance("Tesla", "1800")
    state.save()

    assert RunState(path).since_id("Tesla") == "1800"


def test_since_id_lookup_is_case_insensitive(tmp_path):
    state = RunState(tmp_path / "state.json")
    state.advance("Tesla", "1800")

    assert state.since_id("TESLA") == "1800"


def test_advance_never_moves_backward(tmp_path):
    state = RunState(tmp_path / "state.json")
    state.advance("Tesla", "1800")
    state.advance("Tesla", "1700")

    assert state.since_id("Tesla") == "1800"


def test_advance_ignores_empty_ids(tmp_path):
    state = RunState(tmp_path / "state.json")
    state.advance("Tesla", "1800")
    state.advance("Tesla", None)
    state.advance("Tesla", "")

    assert state.since_id("Tesla") == "1800"


def test_missing_since_id_is_none(tmp_path):
    assert RunState(tmp_path / "state.json").since_id("Tesla") is None


def test_reset_clears_the_mark_but_keeps_dedupe(tmp_path):
    state = RunState(tmp_path / "state.json")
    state.advance("Tesla", "1800")
    state.remember("Tesla", "key")
    state.reset("Tesla")

    assert state.since_id("Tesla") is None
    assert state.is_duplicate("Tesla", "key")


def test_dedupe_ring_round_trips(tmp_path):
    path = tmp_path / "state.json"
    state = RunState(path)
    state.remember("Tesla", "abc")
    state.save()

    reloaded = RunState(path)
    assert reloaded.is_duplicate("Tesla", "abc")
    assert not reloaded.is_duplicate("Tesla", "xyz")
    assert not reloaded.is_duplicate("Rivian", "abc")


def test_dedupe_ignores_empty_keys(tmp_path):
    state = RunState(tmp_path / "state.json")
    state.remember("Tesla", "")

    assert not state.is_duplicate("Tesla", "")


def test_remembering_twice_does_not_grow_the_ring(tmp_path):
    state = RunState(tmp_path / "state.json", seen_limit=3)
    for _ in range(5):
        state.remember("Tesla", "same")
    state.save()

    seen = json.loads((tmp_path / "state.json").read_text())["companies"]["tesla"]["seen"]
    assert seen == ["same"]


def test_dedupe_ring_evicts_oldest_past_the_limit(tmp_path):
    state = RunState(tmp_path / "state.json", seen_limit=3)
    for key in ["a", "b", "c", "d"]:
        state.remember("Tesla", key)

    assert not state.is_duplicate("Tesla", "a")  # evicted
    assert all(state.is_duplicate("Tesla", k) for k in ["b", "c", "d"])


def test_ring_limit_is_applied_on_load(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "version": 1,
        "companies": {"tesla": {"since_id": "1", "seen": ["a", "b", "c", "d", "e"]}},
    }), encoding="utf-8")

    state = RunState(path, seen_limit=2)

    assert not state.is_duplicate("Tesla", "a")
    assert state.is_duplicate("Tesla", "e")


def test_corrupt_state_file_resets_instead_of_crashing(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")

    state = RunState(path)

    assert state.since_id("Tesla") is None


def test_unexpected_state_shape_is_ignored(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"companies": {"tesla": "not-a-dict"}}), encoding="utf-8")

    assert RunState(path).since_id("Tesla") is None


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    state = RunState(tmp_path / "state.json")
    state.advance("Tesla", "1800")
    state.save()

    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_save_creates_missing_directories(tmp_path):
    state = RunState(tmp_path / "nested" / "state.json")
    state.advance("Tesla", "1")
    state.save()

    assert state.path.exists()
