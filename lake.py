from catalog import Catalog
from objects import Objects


class Lake:
    def __init__(self, catalog=None, objects=None):
        self.catalog = catalog or Catalog()
        self.objects = objects or Objects()

    def seed(self, files):
        for fid, rows in files.items():
            self.objects.put(fid, rows)
        with self.catalog.write():
            if self.catalog.head() != 0:
                raise ValueError("ALREADY_SEEDED")
            return self.catalog.commit(set(files), set())

    def read(self, snapshot=None):
        sid, files, dead = self.catalog.state(snapshot)
        rows = []
        for fid in sorted(files):
            for pos, value in enumerate(self.objects.get(fid)):
                if (fid, pos) not in dead:
                    rows.append({"file": fid, "pos": pos, "value": value})
        return {"snapshot": sid, "rows": rows}

    def delete(self, snapshot, fid, pos):
        _, source, _ = self.catalog.state(snapshot)
        if fid not in source or type(pos) is not int or not 0 <= pos < len(self.objects.get(fid)):
            raise ValueError("BAD_POSITION")
        with self.catalog.write():
            sid, current, dead = self.catalog.state()
            if fid not in current:
                raise ValueError("STALE_LAYOUT")
            if (fid, pos) in dead:
                return sid
            return self.catalog.commit(current, dead | {(fid, pos)})

    def prepare(self, job, inputs, layout):
        base, files, dead = self.catalog.state()
        if len(inputs) != len(set(inputs)) or not inputs or not set(inputs) <= files:
            raise ValueError("BAD_INPUTS")
        payload = {fid: self.objects.get(fid) for fid in inputs}
        expected = {(fid, p) for fid in inputs for p in range(len(payload[fid]))}
        requested = [tuple(ref) for group in layout for ref in group]
        if len(requested) != len(set(requested)) or set(requested) != expected:
            raise ValueError("BAD_LAYOUT")
        outputs = []
        for i, group in enumerate(layout):
            fid = "rewrite-" + job + "-" + str(i)
            rows = [payload[f][p] for f, p in group if (f, p) not in dead]
            self.objects.put(fid, rows)
            outputs.append(fid)
        return {"base": base, "inputs": sorted(inputs), "outputs": outputs}

    def publish(self, plan):
        with self.catalog.write():
            sid, files, dead = self.catalog.state()
            if sid != plan["base"]:
                raise ValueError("PUBLISH_CONFLICT")
            inputs = set(plan["inputs"])
            if not inputs <= files:
                raise ValueError("BAD_PLAN")
            return self.catalog.commit(
                (files - inputs) | set(plan["outputs"]),
                {(f, p) for f, p in dead if f not in inputs})

    def rewrite(self, job, inputs, layout):
        with self.catalog.write():
            plan = self.prepare(job, inputs, layout)
            return self.publish(plan)
