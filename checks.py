from lake import Lake
from replay import replay
from samples import FILES, LAYOUT, scenario


def origins(entry):
    return [r["origin"] for r in entry["result"]["rows"]]


def check():
    lake = Lake()
    first = lake.seed(FILES)
    assert first == 1
    assert lake.rewrite("sync", ["a", "b"], LAYOUT) == 2
    assert [r["value"]["reading"] for r in lake.read()["rows"]] == [7, 9, 7, 11]
    assert [r["value"]["reading"] for r in lake.read(first)["rows"]] == [7, 7, 9, 11]
    assert lake.delete(2, "rewrite-sync-0", 0) == 3
    assert [r["value"]["reading"] for r in lake.read()["rows"]] == [9, 7, 11]
    before = replay(scenario("before"))
    assert origins(before[-2]) == ["a:1", "b:0", "b:1"]
    assert origins(before[-1]) == ["a:0", "a:1", "b:0", "b:1"]
    between = replay(scenario("between"))
    assert between[2]["error"] == "PUBLISH_CONFLICT"
    assert origins(between[-2]) == ["a:1", "b:0", "b:1"]
    after = replay(scenario("after"))
    assert after[2]["error"] == "STALE_LAYOUT"
    assert origins(after[-2]) == ["a:1", "b:0", "a:0", "b:1"]
    assert origins(after[-1]) == ["a:0", "a:1", "b:0", "b:1"]
    old_head = lake.catalog.head()
    try:
        lake.delete(2, "a", 99)
    except ValueError as exc:
        assert str(exc) == "BAD_POSITION"
    else:
        raise AssertionError("invalid position accepted")
    assert lake.catalog.head() == old_head
    print("BASELINE_OK: sync rewrite, position delete, immutable history, staged limitations")


if __name__ == "__main__":
    check()
