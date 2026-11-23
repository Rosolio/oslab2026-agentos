"""SQLite is the authority for queues, checkpoints and published results."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, content_hash TEXT NOT NULL,
                    signature TEXT NOT NULL, requirements TEXT NOT NULL, snapshot TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('ready','running','waiting','completed','failed')),
                    stage TEXT NOT NULL DEFAULT 'extract', checkpoint TEXT NOT NULL DEFAULT '{}',
                    created REAL NOT NULL, updated REAL NOT NULL, queue_order INTEGER NOT NULL,
                    retries INTEGER NOT NULL DEFAULT 0, retry_at REAL, error TEXT,
                    result TEXT, UNIQUE(content_hash, signature)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    path TEXT NOT NULL, content_hash TEXT NOT NULL, signature TEXT NOT NULL,
                    task_id TEXT NOT NULL REFERENCES tasks(id), seen REAL NOT NULL,
                    PRIMARY KEY(path, content_hash, signature)
                );
                CREATE TABLE IF NOT EXISTS calls (
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    tool TEXT NOT NULL, started REAL NOT NULL, duration REAL,
                    status TEXT NOT NULL, tokens INTEGER NOT NULL DEFAULT 0, error TEXT
                );
                CREATE INDEX IF NOT EXISTS queue ON tasks(state, queue_order);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _next(db):
        return db.execute("SELECT COALESCE(MAX(queue_order),0)+1 FROM tasks").fetchone()[0]

    def submit(self, source, digest, signature, requirements, snapshot):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM tasks WHERE content_hash=? AND signature=?", (digest, signature)
            ).fetchone()
            reused = row is not None
            if row is None:
                task_id = uuid.uuid4().hex
                db.execute(
                    """INSERT INTO tasks
                    (id,source,content_hash,signature,requirements,snapshot,state,
                     created,updated,queue_order) VALUES(?,?,?,?,?,?,'ready',?,?,?)""",
                    (
                        task_id,
                        source,
                        digest,
                        signature,
                        json.dumps(requirements),
                        snapshot,
                        now,
                        now,
                        self._next(db),
                    ),
                )
            else:
                task_id = row["id"]
            db.execute(
                "INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?)",
                (source, digest, signature, task_id, now),
            )
        return task_id, reused

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return dict(row)

    def tasks(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY created,id")]

    def recover(self):
        with self.connect() as db:
            # Preserve queue order and checkpoint after a crashed worker.
            db.execute(
                "UPDATE tasks SET state='ready',updated=? WHERE state='running'", (time.time(),)
            )
            db.execute(
                """UPDATE calls SET status='interrupted', error='worker interrupted',
                duration=?-started WHERE status='running'""",
                (time.time(),),
            )

    def claim(self, signature, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            due = db.execute(
                """SELECT id FROM tasks WHERE state='waiting' AND retry_at<=?
                AND signature=? ORDER BY retry_at,queue_order""",
                (now, signature),
            ).fetchall()
            for row in due:
                db.execute(
                    "UPDATE tasks SET state='ready',queue_order=?,updated=? WHERE id=?",
                    (self._next(db), now, row["id"]),
                )
            row = db.execute(
                """SELECT id FROM tasks WHERE state='ready' AND signature=?
                ORDER BY queue_order LIMIT 1""",
                (signature,),
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE tasks SET state='running',updated=? WHERE id=?", (now, row["id"]))
            task_id = row["id"]
        return self.get(task_id)

    def checkpoint(self, task_id, stage, data):
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET stage=?,checkpoint=?,updated=? WHERE id=?",
                (stage, json.dumps(data, ensure_ascii=False), time.time(), task_id),
            )

    def finish(self, task_id, result):
        with self.connect() as db:
            db.execute(
                """UPDATE tasks SET state='completed',stage='done',result=?,
                error=NULL,retry_at=NULL,updated=? WHERE id=?""",
                (result, time.time(), task_id),
            )

    def fail(self, task_id, error, transient, config):
        task = self.get(task_id)
        retry = transient and task["retries"] < config.max_retries
        attempts = task["retries"] + int(retry)
        with self.connect() as db:
            db.execute(
                """UPDATE tasks SET state=?,retries=?,retry_at=?,error=?,updated=?
                WHERE id=?""",
                (
                    "waiting" if retry else "failed",
                    attempts,
                    time.time() + config.retry_delay * 2 ** (attempts - 1) if retry else None,
                    str(error)[:2000],
                    time.time(),
                    task_id,
                ),
            )

    def retry(self, task_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """UPDATE tasks SET state='ready',retries=0,retry_at=NULL,
                error=NULL,queue_order=?,updated=? WHERE id=? AND state='failed'""",
                (self._next(db), time.time(), task_id),
            )
            return cursor.rowcount == 1

    def start_call(self, task_id, tool):
        with self.connect() as db:
            return db.execute(
                "INSERT INTO calls(task_id,tool,started,status) VALUES(?,?,?,'running')",
                (task_id, tool, time.time()),
            ).lastrowid

    def end_call(self, call_id, status, tokens=0, error=None):
        with self.connect() as db:
            db.execute(
                "UPDATE calls SET status=?,tokens=?,error=?,duration=?-started WHERE id=?",
                (status, tokens, error, time.time(), call_id),
            )

    def calls(self, task_id):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM calls WHERE task_id=? ORDER BY id", (task_id,))
            ]
