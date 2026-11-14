"""Baseline observations only: this module does not implement a publication protocol."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
from unittest.mock import patch

from catalog import Catalog
from lake import Lake
from objects import Objects

ROWS = [{"reading": 7}, {"reading": 9}]
OTHER = [{"reading": 8}, {"reading": 9}]
CASES = ("same", "different", "cut_write", "before_commit", "after_commit", "order")


def open_lake(root):
    catalog = Catalog(str(root / "catalog.db"))
    catalog.db.execute("PRAGMA journal_mode=DELETE")
    catalog.db.execute("PRAGMA synchronous=FULL")
    return Lake(catalog, Objects(root / "objects"))


def attempt(call):
    try:
        return {"ok": call()}
    except Exception as exc:
        return {"error": type(exc).__name__, "message": str(exc)}


def pause():
    print("PAUSED", flush=True)
    if sys.stdin.readline().strip() != "GO":
        raise RuntimeError("OBSERVER_NO_GO")


class SplitWrite:
    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()

    def write(self, data):
        n = max(1, len(data) // 2)
        self.stream.write(data[:n])
        self.stream.flush()
        pause()
        self.stream.write(data[n:])
        return len(data)


def worker(mode, root):
    root = Path(root)
    if mode == "split":
        original = Path.open

        def hooked(path, mode="r", *args, **kwargs):
            stream = original(path, mode, *args, **kwargs)
            return SplitWrite(stream) if mode == "xb" else stream

        with patch.object(Path, "open", hooked):
            Objects(root / "objects").put("shared", ROWS)
    else:
        lake = open_lake(root)
        original = lake.catalog.commit

        def commit(files, dead):
            if mode == "before_commit":
                pause()
            return original(files, dead)

        lake.catalog.commit = commit
        lake.seed({"shared": ROWS})
        if mode == "after_commit":
            pause()
        lake.catalog.db.close()


def launch(root, mode):
    proc = subprocess.Popen(
        [sys.executable, "-B", str(Path(__file__).resolve()), "worker", mode, str(root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            if not selector.select(10):
                raise RuntimeError("OBSERVER_PAUSE_TIMEOUT")
        line = proc.stdout.readline().strip()
        if line != "PAUSED":
            raise RuntimeError("OBSERVER_WORKER: " + line + " " + proc.stderr.read())
        return proc
    except BaseException:
        proc.kill()
        proc.communicate()
        raise


def finish(proc, kill=False):
    if kill:
        proc.kill()
    out, err = proc.communicate(None if kill else "GO\n", timeout=10)
    if not kill and proc.returncode != 0:
        raise RuntimeError("OBSERVER_WORKER: " + out + err)


def competition(root, name):
    proc = launch(root, "split")
    try:
        objects = Objects(root / "objects")
        path = root / "objects" / "shared.json"
        result = {"visible_prefix": path.read_bytes().decode(),
                  "during_get": attempt(lambda: objects.get("shared"))}
        if name == "cut_write":
            finish(proc, kill=True)
            result["reopened_get"] = attempt(lambda: Objects(root / "objects").get("shared"))
            result["retry"] = attempt(lambda: objects.put("shared", ROWS))
        else:
            rows = ROWS if name == "same" else OTHER
            result["contender"] = attempt(lambda: objects.put("shared", rows))
            finish(proc)
            result["reopened_get"] = attempt(lambda: Objects(root / "objects").get("shared"))
            result["retry"] = attempt(lambda: objects.put("shared", rows))
        return result
    finally:
        if proc.poll() is None:
            finish(proc, kill=True)


def boundary(root, name):
    proc = launch(root, name)
    finish(proc, kill=True)
    lake = open_lake(root)
    try:
        return {"head": lake.catalog.head(), "read": lake.read(),
                "object": attempt(lambda: lake.objects.get("shared")),
                "retry_seed": attempt(lambda: lake.seed({"shared": ROWS})),
                "after_retry": lake.read()}
    finally:
        lake.catalog.db.close()


def order(root):
    lake = open_lake(root)
    events = []
    original_open = Path.open
    original_sync = os.fsync

    class Recorded:
        def __init__(self, stream, fid):
            self.stream, self.fid = stream, fid

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()
            events.append({"op": "object_close", "fid": self.fid,
                           "in_transaction": lake.catalog.db.in_transaction})

        def write(self, payload):
            events.append({"op": "object_write", "fid": self.fid, "bytes": len(payload),
                           "in_transaction": lake.catalog.db.in_transaction})
            return self.stream.write(payload)

    def hooked(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if mode == "xb":
            events.append({"op": "object_open", "fid": path.stem,
                           "in_transaction": lake.catalog.db.in_transaction})
            return Recorded(stream, path.stem)
        return stream

    def synced(fd):
        events.append({"op": "python_fsync"})
        return original_sync(fd)

    def sql(statement):
        if statement in ("BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"):
            events.append({"op": statement})

    lake.catalog.db.set_trace_callback(sql)
    try:
        with patch.object(Path, "open", hooked), patch.object(os, "fsync", synced):
            lake.seed({"a": ROWS})
            plan = lake.prepare("staged", ["a"], [[["a", 1], ["a", 0]]])
            lake.publish(plan)
            lake.rewrite("sync", ["rewrite-staged-0"],
                         [[["rewrite-staged-0", 0], ["rewrite-staged-0", 1]]])
        return {"events": events, "current": lake.read(), "history": lake.read(1)}
    finally:
        lake.catalog.db.close()


def run(name):
    if name not in CASES:
        raise ValueError("BAD_CASE")
    with tempfile.TemporaryDirectory(prefix="lake-observe-") as directory:
        root = Path(directory)
        if name in ("same", "different", "cut_write"):
            return competition(root, name)
        if name == "order":
            return order(root)
        return boundary(root, name)


def main(argv):
    if len(argv) == 3 and argv[0] == "worker":
        worker(argv[1], argv[2])
        return
    names = CASES if argv == ["all"] else argv
    if not names:
        raise SystemExit("usage: observe.py all|CASE")
    for name in names:
        print(json.dumps({"case": name, "actual": run(name)}, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1:])
