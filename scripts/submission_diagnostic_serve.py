"""仅独立测试目录：记录连接/SQL/提交/写入排队耗时，不记录SQL正文或身份。"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not ROOT.name.startswith("courseloop-submission-test-"):
    raise RuntimeError("诊断服务器只允许隔离测试目录")

from backend.core import db
from scripts.submission_serve import app

_samples = {}
_guard = threading.Lock()

def record(name, started):
    elapsed = (time.perf_counter() - started) * 1000
    with _guard:
        _samples.setdefault(name, []).append(elapsed)

class TimedConnection(sqlite3.Connection):
    def execute(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return super().execute(*args, **kwargs)
        finally:
            record("execute_ms", started)

    def commit(self):
        started = time.perf_counter()
        try:
            return super().commit()
        finally:
            record("commit_ms", started)

    def close(self):
        started = time.perf_counter()
        try:
            return super().close()
        finally:
            record("close_ms", started)

def connect():
    started = time.perf_counter()
    connection = sqlite3.connect(db.settings.db_path, factory=TimedConnection)
    connection.row_factory = sqlite3.Row
    record("connect_ms", started)
    return connection

original_transaction = db.transaction

@contextmanager
def transaction():
    started = time.perf_counter()
    with original_transaction() as connection:
        record("write_wait_begin_ms", started)
        body_start = time.perf_counter()
        try:
            yield connection
        finally:
            record("write_body_ms", body_start)
            finish_start = time.perf_counter()
    record("write_finish_ms", finish_start)
    record("write_total_ms", started)

db._connect = connect
db.transaction = transaction

def flush():
    while True:
        time.sleep(10)
        with _guard:
            snapshot = {key: sorted(values) for key,values in _samples.items()}
        result = {key:dict(count=len(values), mean=sum(values)/len(values),
                  p50=values[int((len(values)-1)*.5)], p95=values[int((len(values)-1)*.95)],
                  maximum=values[-1]) for key,values in snapshot.items() if values}
        destination = ROOT / f"evidence/performance-diagnostics-{os.getpid()}.json"
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(result,indent=2))

threading.Thread(target=flush,daemon=True).start()
