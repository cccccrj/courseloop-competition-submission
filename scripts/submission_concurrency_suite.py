"""独立测试提交版：功能、同账号、10/25/50用户串行验收，保留每阶段状态。"""
import argparse
import fcntl
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def source_manifest():
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "backend").rglob("*.py"))}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    if not ROOT.name.startswith("courseloop-submission-test-"):
        raise SystemExit("只允许独立测试目录")
    if not args.base.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise SystemExit("只允许回环地址")
    evidence = ROOT / "evidence"
    evidence.mkdir(exist_ok=True)
    guard = (ROOT / ".private/concurrency-suite.lock").open("a")
    fcntl.flock(guard,fcntl.LOCK_EX | fcntl.LOCK_NB)
    initial = source_manifest()
    state = {"started_at":datetime.now(timezone.utc).isoformat(),
             "backend_source_sha256":initial, "stages":[], "complete":False}
    def save():
        (evidence / "concurrency-suite.json").write_text(json.dumps(state,indent=2))
    save()
    phases = [("functional", ["scripts/submission_test.py","functional","--isolated",
               "--accounts", ".private/accounts.json"]),
              ("same-user", ["scripts/submission_same_user.py", "--rounds","5"])]
    phases += [(f"load-{users}", ["scripts/submission_test.py","load","--isolated",
               "--accounts", ".private/load-accounts.json", "--users",str(users),
               "--duration",str(duration)]) for users,duration in ((10,60),(25,120),(50,1800))]
    for name, command in phases:
        stage = {"name":name,"started_at":datetime.now(timezone.utc).isoformat(),"complete":False}
        state["stages"].append(stage)
        save()
        result = subprocess.run([sys.executable,*command,"--base",args.base,
                                 "--out",str(evidence / f"{name}-final.json")],cwd=ROOT)
        stage.update(exit_code=result.returncode,complete=True,
                     finished_at=datetime.now(timezone.utc).isoformat())
        state["backend_source_unchanged"] = source_manifest() == initial
        save()
        if result.returncode or not state["backend_source_unchanged"]:
            raise SystemExit(result.returncode or 1)
    state["complete"] = True
    state["finished_at"] = datetime.now(timezone.utc).isoformat()
    save()

if __name__ == "__main__":
    main()
