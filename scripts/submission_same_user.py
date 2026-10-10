"""隔离环境同一学生多设备并发：分别核对记录完整性与掌握度累积一致性。"""
import concurrent.futures
import argparse
import json
import sys
from pathlib import Path
import httpx
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8015/api")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--out", type=Path, default=ROOT / "evidence/same-user-concurrency.json")
    args = parser.parse_args()
    if not ROOT.name.startswith("courseloop-submission-test-"):
        raise SystemExit("只允许测试环境独立测试目录")
    if not args.base.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise SystemExit("只允许回环入口")
    from backend.core import db
    from backend.learning.mastery import update_mastery
    from scripts.submission_test import gold_problem, login
    p = gold_problem()
    full = db.get_problem(p["id"],with_solution=True)
    rounds = []
    for _ in range(args.rounds):
        # 每轮新身份且固定初始值，避免掌握度饱和到1.0后掩盖丢更新。
        user = dict(username="competition-same-" + uuid.uuid4().hex[:12],
                    password=uuid.uuid4().hex, role="student")
        db.register_user(user["username"], user["password"], "虚构并发一致性", role="student")
        db.upsert_mastery(user["username"], p["knowledge_id"], .2618, .1, "learning")
        rounds.append(run_round(args, db, update_mastery, login, p, user, full))
    result = dict(rounds=rounds, total_requests=sum(r["requests"] for r in rounds),
                  all_attempts_complete=all(r["attempts_complete"] for r in rounds),
                  all_mastery_consistent=all(r["mastery_serial_consistent"] for r in rounds))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!="rounds"},ensure_ascii=False))
    raise SystemExit(0 if result["all_attempts_complete"] and result["all_mastery_consistent"] else 1)


def run_round(args, db, update_mastery, login, p, user, full):
    with httpx.Client(base_url=args.base,timeout=30) as c:
        header = login(c,user)
    with db.get_conn() as c:
        before = c.execute("SELECT COUNT(*) FROM practice_attempts WHERE username=?",(user["username"],)).fetchone()[0]
    prev = db.get_mastery(user["username"],p["knowledge_id"])
    expected = prev["mastery"] if prev else 0.0
    for _ in range(20):
        expected = update_mastery(expected,True,difficulty=full["difficulty"])
    def submit(_):
        with httpx.Client(base_url=args.base,timeout=30,headers=header) as c:
            r = c.post("/problems/judge",json=dict(problem_id=p["id"],course=p["course"],answer=p["value"]))
            return dict(status=r.status_code,passed=r.json().get("passed"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(submit,range(20)))
    with db.get_conn() as c:
        after = c.execute("SELECT COUNT(*) FROM practice_attempts WHERE username=?",(user["username"],)).fetchone()[0]
    actual = db.get_mastery(user["username"],p["knowledge_id"])["mastery"]
    result = dict(environment="isolated same synthetic student, 10 parallel clients / 20 correct answers",
                  requests=20,successful=sum(r["status"]==200 and r["passed"] is True for r in responses),
                  persisted_attempt_delta=after-before,attempts_complete=after-before==20,
                  mastery_before=prev["mastery"] if prev else 0,expected_serial_mastery=expected,
                  actual_mastery=actual,mastery_serial_consistent=abs(actual-expected)<.0001,
                  method="fresh identity per round, initial mastery .2618, 10 clients / 20 correct answers",
                  responses=responses)
    return result


if __name__=="__main__":
    main()
