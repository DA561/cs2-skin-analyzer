"""Bounded SQLite job queue. One worker process/replica shares the provider limiter."""

import asyncio
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .models import ItemsRequest
from .providers import SourceError
from .report import build_report


class Jobs:
    def __init__(self, csqaq, youpin, vision):
        self.path = os.getenv("ANALYZER_DB_PATH", "/tmp/cs2-analyzer.sqlite3")
        self.csqaq, self.youpin, self.vision = csqaq, youpin, vision
        self.images = {}
        self.active = None
        self.last_cleanup = 0.0

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def initialize(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connection() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT, fingerprint TEXT, status TEXT, created REAL, updated REAL, payload TEXT, result TEXT, error TEXT)"
            )
            con.execute(
                "UPDATE jobs SET status='interrupted', error='worker_restarted', payload=NULL WHERE status IN ('queued','running')"
            )

    def submit(self, owner, request=None, raw=None, expected_count=30):
        payload = request.model_dump_json() if request else json.dumps({"expected_count": expected_count})
        digest = hashlib.sha256((payload + ("image" if raw else "items")).encode() + (raw or b"")).hexdigest()
        at = time.time()
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM jobs WHERE updated < ? AND status NOT IN (?,?)", (at - 86400, "queued", "running"))
            cached = con.execute(
                "SELECT id,status FROM jobs WHERE owner=? AND fingerprint=? AND created>? AND status IN ('queued','running','completed') ORDER BY created DESC LIMIT 1",
                (owner, digest, at - 300),
            ).fetchone()
            if cached:
                return {"job_id": cached["id"], "status": cached["status"], "cached": True}
            if con.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 8:
                raise SourceError("job_queue_full")
            job_id = uuid.uuid4().hex
            con.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, owner, digest, "queued", at, at, payload, None, None),
            )
        if raw:
            self.images[job_id] = raw
        return {"job_id": job_id, "status": "queued", "cached": False}

    def get(self, job_id, owner):
        with self.connection() as con:
            row = con.execute(
                "SELECT * FROM jobs WHERE id=? AND owner=? AND updated>?", (job_id, owner, time.time() - 86400)
            ).fetchone()
        if not row:
            return None
        return {
            "job_id": job_id,
            "status": row["status"],
            "created_at_unix": row["created"],
            "updated_at_unix": row["updated"],
            "report": json.loads(row["result"]) if row["result"] else None,
            "error": row["error"],
        }

    async def run_one(self):
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return False
            con.execute("UPDATE jobs SET status='running',updated=? WHERE id=?", (time.time(), row["id"]))
        self.active = row["id"]
        raw = self.images.pop(row["id"], None)
        try:
            if raw:
                request = await self.vision.recognize(raw, json.loads(row["payload"])["expected_count"])
            else:
                request = ItemsRequest.model_validate_json(row["payload"])
            raw = None
            report = await build_report(request, self.csqaq, self.youpin)
            with self.connection() as con:
                con.execute(
                    "UPDATE jobs SET status='completed',updated=?,payload=NULL,result=? WHERE id=?",
                    (time.time(), json.dumps(report, ensure_ascii=False, allow_nan=False), row["id"]),
                )
        except asyncio.CancelledError:
            with self.connection() as con:
                con.execute(
                    "UPDATE jobs SET status='interrupted',error='worker_stopped',payload=NULL,updated=? WHERE id=?",
                    (time.time(), row["id"]),
                )
            raise
        except Exception as exc:
            error = exc.code if isinstance(exc, SourceError) else "analysis_internal_error"
            with self.connection() as con:
                con.execute(
                    "UPDATE jobs SET status='failed',error=?,payload=NULL,updated=? WHERE id=?",
                    (error, time.time(), row["id"]),
                )
        finally:
            self.active = None
        return True

    async def worker(self):
        while True:
            if time.monotonic() - self.last_cleanup > 60:
                with self.connection() as con:
                    con.execute(
                        "DELETE FROM jobs WHERE updated < ? AND status NOT IN (?,?)",
                        (time.time() - 86400, "queued", "running"),
                    )
                self.last_cleanup = time.monotonic()
            if not await self.run_one():
                await asyncio.sleep(0.5)
