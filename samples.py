FILES = {
    "a": [{"reading": 7}, {"reading": 7}],
    "b": [{"reading": 9}, {"reading": 11}],
}
LAYOUT = [[["a", 1], ["b", 0]], [["a", 0], ["b", 1]]]


def scenario(name):
    prepare = {"op": "prepare", "job": "r", "inputs": ["a", "b"], "layout": LAYOUT}
    delete = {"op": "delete", "snapshot": "initial", "file": "a", "pos": 0}
    read = {"op": "read"}
    history = {"op": "read", "snapshot": "initial"}
    sequences = {
        "normal": [prepare, {"op": "publish"}, read, history],
        "before": [delete, prepare, {"op": "publish"}, read, history],
        "between": [prepare, delete, {"op": "publish"}, read, history],
        "after": [prepare, {"op": "publish"}, delete, read, history],
    }
    return {"files": FILES, "operations": sequences[name]}
