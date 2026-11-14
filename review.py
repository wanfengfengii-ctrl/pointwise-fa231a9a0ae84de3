"""Business regression and evidence replay; protocol correctness still needs review."""
import json
from pathlib import Path
import re
import sys

from checks import check
from observe import CASES, ROWS, run


def baseline():
    check()
    actual = {name: run(name) for name in CASES}
    for name in ("same", "different", "cut_write"):
        assert actual[name]["during_get"]["error"] == "JSONDecodeError"
    for name in ("different", "cut_write"):
        assert actual[name]["retry"]["message"] == "IMMUTABLE_CONFLICT"
    assert actual["same"]["contender"]["message"] == "IMMUTABLE_CONFLICT"
    assert actual["same"]["retry"] == {"ok": None}
    assert actual["different"]["contender"]["message"] == "IMMUTABLE_CONFLICT"
    assert actual["cut_write"]["reopened_get"]["error"] == "JSONDecodeError"
    for name, head in (("before_commit", 0), ("after_commit", 1)):
        assert actual[name]["head"] == head
        assert actual[name]["object"] == {"ok": ROWS}
        assert actual[name]["after_retry"]["snapshot"] == 1
    assert actual["before_commit"]["read"]["rows"] == []
    assert actual["after_commit"]["retry_seed"]["message"] == "ALREADY_SEEDED"
    events = actual["order"]["events"]
    writes = [e for e in events if e["op"] == "object_write"]
    assert [e["in_transaction"] for e in writes] == [False, False, True]
    assert not any(e["op"] == "python_fsync" for e in events)
    assert [r["value"] for r in actual["order"]["history"]["rows"]] == ROWS
    assert [r["value"] for r in actual["order"]["current"]["rows"]] == list(reversed(ROWS))
    print("PUBLICATION_BASELINE_OK: observed current behavior, not a safe protocol")
    return actual


def verify(actual):
    text = Path("DESIGN.md").read_text(encoding="utf-8")
    match = re.search(r"<!-- publication-evidence -->\s*~~~json\s*(.*?)\s*~~~", text, re.S)
    if not match:
        raise AssertionError("MISSING_PUBLICATION_EVIDENCE")
    evidence = json.loads(match.group(1))
    assert set(evidence) == set(CASES)
    for name in CASES:
        entry = evidence[name]
        assert entry["actual"] == actual[name], name
        assert isinstance(entry["predicted"], str) and entry["predicted"].strip(), name
        assert isinstance(entry["basis"], str) and entry["basis"].strip(), name
    for filename in ("objects.py", "catalog.py", "lake.py"):
        lines = Path(filename).read_text().splitlines()
        refs = re.findall(re.escape(filename) + r":([1-9][0-9]*)", text)
        assert refs and all(1 <= int(n) <= len(lines) for n in refs), filename
    print("DESIGN_EVIDENCE_OK: observations and source locations replayed; design needs review")


if __name__ == "__main__":
    if sys.argv[1:] not in (["baseline"], ["verify"]):
        raise SystemExit("usage: review.py baseline|verify")
    results = baseline()
    if sys.argv[1] == "verify":
        verify(results)
