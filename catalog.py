import sqlite3
from contextlib import contextmanager


class Catalog:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots(sid INTEGER PRIMARY KEY, parent INTEGER);
            CREATE TABLE IF NOT EXISTS files(
                sid INTEGER, fid TEXT, PRIMARY KEY(sid,fid),
                FOREIGN KEY(sid) REFERENCES snapshots(sid));
            CREATE TABLE IF NOT EXISTS tombstones(
                sid INTEGER, fid TEXT, pos INTEGER, PRIMARY KEY(sid,fid,pos),
                FOREIGN KEY(sid,fid) REFERENCES files(sid,fid));
            CREATE TABLE IF NOT EXISTS head(singleton INTEGER PRIMARY KEY CHECK(singleton=1), sid INTEGER);
            INSERT OR IGNORE INTO snapshots VALUES(0,NULL);
            INSERT OR IGNORE INTO head VALUES(1,0);
        """)

    @contextmanager
    def write(self):
        nested = self.db.in_transaction
        if not nested:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            if not nested:
                self.db.execute("COMMIT")
        except BaseException:
            if not nested:
                self.db.execute("ROLLBACK")
            raise

    def head(self):
        return self.db.execute("SELECT sid FROM head WHERE singleton=1").fetchone()[0]

    def state(self, sid=None):
        sid = self.head() if sid is None else sid
        if not self.db.execute("SELECT 1 FROM snapshots WHERE sid=?", (sid,)).fetchone():
            raise ValueError("NO_SNAPSHOT")
        files = {r[0] for r in self.db.execute("SELECT fid FROM files WHERE sid=?", (sid,))}
        dead = {(r[0], r[1]) for r in self.db.execute(
            "SELECT fid,pos FROM tombstones WHERE sid=?", (sid,))}
        return sid, files, dead

    def commit(self, files, dead):
        if not self.db.in_transaction:
            raise RuntimeError("WRITE_TRANSACTION_REQUIRED")
        parent = self.head()
        sid = parent + 1
        self.db.execute("INSERT INTO snapshots VALUES(?,?)", (sid, parent))
        self.db.executemany("INSERT INTO files VALUES(?,?)", [(sid, f) for f in sorted(files)])
        self.db.executemany("INSERT INTO tombstones VALUES(?,?,?)",
                            [(sid, f, p) for f, p in sorted(dead)])
        self.db.execute("UPDATE head SET sid=? WHERE singleton=1", (sid,))
        return sid
