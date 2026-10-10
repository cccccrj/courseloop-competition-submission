"""Exercise processed delivery data in a disposable, loopback-only runtime.

Runs role/function/collaboration checks and five same-user concurrency rounds.
Only copies exported databases; no writes or model requests reach live services.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(data, output):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable).absolute()
    with tempfile.TemporaryDirectory(prefix="courseloop-submission-test-processed-") as temporary:
        root = Path(temporary)
        tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
        for name in filter(None, tracked):
            src = ROOT / name
            if src.is_file():
                dst = root / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
        shutil.copytree(ROOT / "frontend/dist", root / "frontend/dist")
        shutil.copytree(data, root / "data/processed")
        (root / ".private").mkdir()
        shutil.copyfile(ROOT / ".private/accounts.json", root / ".private/accounts.json")
        env = {k: v for k, v in os.environ.items() if not k.startswith("CL_") and k != "PYTHONPATH"}
        env.update(CL_DB_PATH=str(root / "data/processed/courseloop.db"),
                   CL_RAG_FTS_PATH=str(root / "data/processed/rag_fts.db"), CL_RAG_MODE="fts5",
                   CL_SECRET_KEY=secrets.token_hex(32), CL_LLM_API_KEY="", CL_LLM_BASE_URL="",
                   CL_LOGS_DIR=str(root / "data/logs"), CL_RAW_DIR=str(root / "data/raw"))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}/api"
        with (output / "server.log").open("wb") as log:
            process = subprocess.Popen([str(python), "-m", "uvicorn", "scripts.submission_serve:app",
                                        "--host", "127.0.0.1", "--port", str(port)],
                                       cwd=root, env=env, stdout=log, stderr=log)
            try:
                for _ in range(120):
                    if process.poll() is not None:
                        raise RuntimeError("Disposable service exited during startup")
                    try:
                        urllib.request.urlopen(base + "/health", timeout=1).close()
                        break
                    except (OSError, TimeoutError):
                        time.sleep(.25)
                else:
                    raise RuntimeError("Disposable service startup timeout")
                subprocess.run([str(python), "scripts/submission_test.py", "functional", "--isolated",
                                "--base", base, "--out", str(output / "functional.json")],
                               cwd=root, env=env, check=True)
                subprocess.run([str(python), "scripts/submission_same_user.py", "--rounds", "5",
                                "--base", base, "--out", str(output / "same-user.json")],
                               cwd=root, env=env, check=True)
                # Source paths retained in citations are metadata: FTS reads do
                # not need the source-host filesystem to answer these queries.
                code = (
                    "import json; from backend.rag.retriever import get_engine; "
                    "e=get_engine(); pairs=[('ds-algo','栈与队列'),('co','Cache'),('os','LRU'),('net','TCP 三次握手')]; "
                    "rows=[{'course':c,'query':q,'hits':len(e.retrieve(q,course=c,top_n=3))} for c,q in pairs]; "
                    "assert all(r['hits'] for r in rows); print(json.dumps(rows,ensure_ascii=False))"
                )
                result = subprocess.check_output([str(python), "-c", code], cwd=root, env=env, text=True)
                retrieval = json.loads(result.strip().splitlines()[-1])
                functional = json.loads((output / "functional.json").read_text())
                same = json.loads((output / "same-user.json").read_text())
                manifest = json.loads((data / "DATA_MANIFEST.json").read_text())
                summary = dict(scope="local disposable processed-content runtime; CPU/SQLite; no LLM calls",
                               problems=manifest["content_tables"]["problems"], chunks=manifest["retrieval_chunks"],
                               functional_checks=len(functional["checks"]),
                               functional_passed=sum(x["ok"] for x in functional["checks"]),
                               concurrent_requests=same["total_requests"],
                               all_attempts_complete=same["all_attempts_complete"],
                               all_mastery_consistent=same["all_mastery_consistent"],
                               retrieval_checks=retrieval, passed=True)
                (output / "acceptance.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
                return summary
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.data.resolve(), args.out), ensure_ascii=False))
