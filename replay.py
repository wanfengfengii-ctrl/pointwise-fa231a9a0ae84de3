import json
import sys
from lake import Lake
from samples import scenario


def replay(spec):
    lake = Lake()
    initial = lake.seed(spec["files"])
    # Audit labels are external to lake files and catalog.
    origin = {(fid, pos): f"{fid}:{pos}"
              for fid, rows in spec["files"].items() for pos in range(len(rows))}
    plan = None
    trace = []
    for operation in spec["operations"]:
        op = operation["op"]
        record = {"op": op}
        try:
            if op == "delete":
                snap = operation["snapshot"]
                record["snapshot"] = lake.delete(
                    initial if snap == "initial" else snap, operation["file"], operation["pos"])
            elif op == "prepare":
                base, _, dead = lake.catalog.state()
                plan = lake.prepare(operation["job"], operation["inputs"], operation["layout"])
                for fid, group in zip(plan["outputs"], operation["layout"]):
                    surviving = [tuple(ref) for ref in group if tuple(ref) not in dead]
                    for pos, old in enumerate(surviving):
                        origin[fid, pos] = origin[old]
                record["plan"] = plan
            elif op == "publish":
                if plan is None:
                    raise ValueError("NO_PLAN")
                record["snapshot"] = lake.publish(plan)
            elif op == "read":
                snap = operation.get("snapshot")
                result = lake.read(initial if snap == "initial" else snap)
                for row in result["rows"]:
                    row["origin"] = origin[row["file"], row["pos"]]
                record["result"] = result
            else:
                raise ValueError("BAD_OPERATION")
        except ValueError as exc:
            record["error"] = str(exc)
        trace.append(record)
    return trace


def main(argv):
    if len(argv) == 2 and argv[0] == "--input":
        with open(argv[1], encoding="utf-8") as stream:
            spec = json.load(stream)
        print(json.dumps(replay(spec), ensure_ascii=False, indent=2))
    elif len(argv) == 1 and argv[0] == "all":
        for name in ("normal", "before", "between", "after"):
            print(json.dumps({"case": name, "trace": replay(scenario(name))}, ensure_ascii=False))
    elif len(argv) == 1:
        print(json.dumps(replay(scenario(argv[0])), ensure_ascii=False, indent=2))
    else:
        raise SystemExit("usage: replay.py normal|before|between|after|all OR --input FILE")


if __name__ == "__main__":
    main(sys.argv[1:])
