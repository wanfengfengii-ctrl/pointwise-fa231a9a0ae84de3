import json
from pathlib import Path


class Objects:
    """Immutable JSON row files; memory mode uses the same canonical bytes."""
    def __init__(self, root=None):
        self.root = Path(root) if root else None
        self.memory = {}
        if self.root:
            self.root.mkdir(parents=True, exist_ok=True)

    def put(self, fid, rows):
        if not fid or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in fid):
            raise ValueError("BAD_FILE_ID")
        payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if self.root:
            path = self.root / (fid + ".json")
            try:
                with path.open("xb") as stream:
                    stream.write(payload)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise ValueError("IMMUTABLE_CONFLICT")
        elif fid in self.memory and self.memory[fid] != payload:
            raise ValueError("IMMUTABLE_CONFLICT")
        else:
            self.memory[fid] = payload

    def get(self, fid):
        payload = (self.root / (fid + ".json")).read_bytes() if self.root else self.memory[fid]
        return json.loads(payload)

