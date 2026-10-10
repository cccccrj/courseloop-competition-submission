"""可复现 HTTP 验收、跨角色协同与有界并发；原始令牌/密码不写报告。"""
from __future__ import annotations
import argparse
import fcntl
import concurrent.futures
import json
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def login(client, row):
    r = client.post("/auth/login", json={"username": row["username"], "password": row["password"]})
    r.raise_for_status()
    body = r.json()
    assert body["user"]["role"] == row["role"]
    return {"Authorization": "Bearer " + body["token"]}


def gold_problem():
    from backend.core import db
    with db.get_conn() as c:
        for r in c.execute("SELECT id,course_id,answer FROM problems WHERE verification_type='numeric' ORDER BY id"):
            try:
                a = json.loads(r["answer"])
                if a.get("kind") == "choice" and isinstance(a.get("value"), str):
                    kid, _ = db.knowledge_id_of_problem(r["id"])
                    return dict(id=r["id"], course=r["course_id"], value=a["value"], knowledge_id=kid)
            except (ValueError, AttributeError):
                continue
    raise RuntimeError("无可确定性判定的单选题")


def functional(a, rows):
    checks, cases = [], {}
    with httpx.Client(base_url=a.base, timeout=180) as c:
        def check(name, method, path, body=None, headers=None, status=200, predicate=None):
            t = time.perf_counter()
            try:
                r = c.request(method, path, json=body, headers=headers)
                assert r.status_code == status, f"expected {status}, got {r.status_code}"
                data = r.json() if "json" in r.headers.get("content-type", "") else {}
                if predicate:
                    assert predicate(data), "response invariant failed"
                checks.append(dict(name=name, ok=True, status=r.status_code,
                                   ms=round((time.perf_counter()-t)*1000, 2)))
                return data
            except Exception as exc:
                checks.append(dict(name=name, ok=False, error=type(exc).__name__, detail=str(exc)[:160]))
                return {}
        for path in ("/health", "/courses", "/error-tags"):
            check("公开只读 " + path, "GET", path)
        for path in ("/auth/profile", "/sessions", "/records/errors", "/records/mastery"):
            check("匿名拒绝 " + path, "GET", path, status=401)
        for path in ("/admin/runtime", "/admin/users", "/teaching/errors"):
            check("匿名高权限拒绝 " + path, "GET", path, status=403)
        for path in ("/docs", "/openapi.json", "/redoc"):
            check("不公开接口文档 " + path, "GET", path, status=404)
        check("错误密码拒绝", "POST", "/auth/login",
              {"username": rows[0]["username"], "password": "intentional-invalid-password"}, status=401)
        stu = login(c, rows[0])
        for course in ("ds-algo", "co", "os", "net"):
            for endpoint in ("knowledge", "problems", "practice/recommend", "learning/center",
                             "records/mastery", "records/errors", "profile/weak", "placement/status"):
                check(f"{course} {endpoint}", "GET", f"/{endpoint}?course={course}", headers=stu)
        for path in ("/study/budget", "/exam/preview", "/exam/history", "/review/plan/active?course=os",
                     "/report/weekly?course=os&with_comment=false", "/community/standings?course=os", "/sessions"):
            check("学生功能 " + path, "GET", path, headers=stu)
        for path in ("/admin/runtime", "/admin/users", "/teaching/overview", "/teaching/heatmap"):
            check("学生越权拒绝 " + path, "GET", path, headers=stu, status=403)
        check("伪造令牌拒绝", "GET", "/auth/profile",
              headers={"Authorization": "Bearer not-a-valid-token"}, status=401)
        check("客户端角色头不能提权", "GET", "/admin/users",
              headers={"X-Role": "admin", "X-User": "demo_ta"}, status=403)
        if not a.isolated:
            return dict(environment="public production HTTPS (read-only except login)",
                        checks=checks, cases=cases)
        # 所有写入及多角色读仅允许明确的隔离模式。
        if not a.base.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise SystemExit("隔离写测试只允许 localhost")
        other = login(c, rows[1])
        ta = login(c, next(x for x in rows if x["role"] == "ta"))
        teacher = login(c, next(x for x in rows if x["role"] == "teacher"))
        admin = login(c, next(x for x in rows if x["role"] == "admin"))
        check("教务运行态", "GET", "/admin/runtime", headers=admin)
        for role, header in (("助教", ta), ("教师", teacher)):
            for path in ("/teaching/overview", "/teaching/errors", "/teaching/heatmap", "/teaching/today"):
                check(role + path, "GET", path, headers=header)
        p = gold_problem()
        wrong = next(x for x in "ABCD" if x != p["value"])
        before = check("写入前学情", "GET", f"/learning/center?course={p['course']}", headers=stu)
        incorrect = check("确定性判分：错答", "POST", "/problems/judge",
                          dict(problem_id=p["id"], answer=wrong, course=p["course"]), stu,
                          predicate=lambda d: d.get("passed") is False)
        errors = check("错答进入错题本", "GET", f"/records/errors?course={p['course']}", headers=stu,
                       predicate=lambda d: bool(d.get("items")))
        rid = errors["items"][0]["id"] if errors.get("items") else 0
        check("其他学生看不到我的错题", "GET", f"/records/errors?course={p['course']}", headers=other,
              predicate=lambda d: rid not in [x["id"] for x in d.get("items", [])])
        check("学生申请人工复核", "POST", "/review-requests", {"error_record_id": rid}, stu)
        queue = check("助教看到申请上下文", "GET", f"/teaching/errors?course={p['course']}", headers=ta,
                      predicate=lambda d: any(x["id"] == rid for x in d.get("items", [])))
        review = check("教师确认错因", "POST", "/teaching/review-error",
                       dict(record_id=rid, verdict="confirm", note="比赛隔离测试：核对选项与定义"), teacher)
        check("学生读回教师反馈", "GET", f"/records/errors?course={p['course']}", headers=stu,
              predicate=lambda d: any(x["id"] == rid and x.get("ta_note") for x in d.get("items", [])))
        correct = check("同题纠正并复测", "POST", "/problems/judge",
                        dict(problem_id=p["id"], answer=p["value"], course=p["course"]), stu,
                        predicate=lambda d: d.get("passed") is True)
        after = check("复测后学情", "GET", f"/learning/center?course={p['course']}", headers=stu)
        plan = check("复习排期", "POST", "/review/plan",
                     dict(course=p["course"], days_to_exam=30, daily_minutes=30), stu)
        assignment = check("助教布置针对性作业", "POST", "/teaching/assignments",
                           dict(course=p["course"], title="比赛隔离作业", knowledge_ids=[p["knowledge_id"]],
                                target_count=1, class_name=""), ta)
        check("学生看到布置的作业", "GET", f"/assignments?course={p['course']}", headers=stu,
              predicate=lambda d: bool(d.get("items")))
        check("完成作业真实作答", "POST", "/problems/judge",
              dict(problem_id=p["id"], answer=p["value"], course=p["course"]), stu)
        cases["collaboration"] = dict(problem_id=p["id"], wrong_answer=wrong, correct_answer=p["value"],
                                       incorrect=incorrect, correction=correct, review=review,
                                       plan=plan, assignment=assignment,
                                       attempts_before=before.get("overview", {}).get("attempts"),
                                       attempts_after=after.get("overview", {}).get("attempts"))
        # 非 LLM 的会话归属：直接创建持久化记录，API 两个真实身份验证。
        from backend.core import db
        sid = db.create_session(rows[0]["username"], course_id="os", intent="chat")
        db.save_turn(sid, "student", "合成隔离测试会话")
        check("会话持久化读回", "GET", f"/sessions/{sid}", headers=stu)
        check("其他学生不可读会话", "GET", f"/sessions/{sid}", headers=other, status=404)
        check("其他学生不可删会话", "DELETE", f"/sessions/{sid}", headers=other, status=404)
        # 使用独立用户测试停用即时生效，不修改比赛学生/教师身份。
        name = "competition-disabled-probe"
        if db.user_status(name) is None:
            db.register_user(name, "local-isolation-probe", role="student")
        else:
            db.set_user_status(name, "active")
        h = login(c, dict(username=name, password="local-isolation-probe", role="student"))
        db.set_user_status(name, "disabled")
        check("停用令牌即时失效", "GET", "/auth/profile", headers=h, status=401)
        with db.get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0]
        return dict(environment="isolated deployment / synthetic identities", problem_count=count, checks=checks, cases=cases)


def percentile(values, q):
    v = sorted(values)
    i = (len(v)-1)*q
    lo, hi = int(i), min(int(i)+1, len(v)-1)
    return round(v[lo]+(v[hi]-v[lo])*(i-lo), 2) if v else None


def load(a, rows):
    if not a.isolated or not ROOT.name.startswith("courseloop-submission-test-"):
        raise SystemExit("压力写入仅允许独立测试目录 --isolated")
    guard = (ROOT / ".private/load.lock").open("a")
    try:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("另一批压力测试正在执行；拒绝重叠导致写入对账失真")
    identities = [x for x in rows if x["username"].startswith("competition-load-")][:a.users]
    if len(identities) != a.users:
        raise SystemExit("独立用户数量不足")
    p = gold_problem()
    tokens = []
    with httpx.Client(base_url=a.base, timeout=30) as c:
        tokens = [login(c, row) for row in identities]
    from backend.core import db
    with db.get_conn() as c:
        before = dict(c.execute("SELECT username,COUNT(*) FROM practice_attempts GROUP BY username"))
    start = time.perf_counter()
    until = start+a.duration
    def worker(index):
        latencies, failures, status, writes, totals = [], [], Counter(), 0, Counter()
        endpoint_times = {}
        paths = ["/auth/profile", "/knowledge?course=os", "/problems?course=os", "/learning/center?course=os",
                 "/records/mastery?course=os", "/records/errors?course=os", "/practice/recommend?course=os"]
        with httpx.Client(base_url=a.base, timeout=30, headers=tokens[index]) as c:
            turn = index
            while time.perf_counter() < until:
                t = time.perf_counter()
                writing = turn % 5 == 0  # 20% 确定性判分写入，不发 LLM 请求
                path = "/problems/judge" if writing else paths[turn % len(paths)]
                totals[path] += 1
                try:
                    r = c.post(path, json=dict(problem_id=p["id"], answer=p["value"], course=p["course"])) \
                        if writing else c.get(path)
                    status[str(r.status_code)] += 1
                    ok = r.status_code == 200 and (not writing or r.json().get("passed") is True)
                    if not ok:
                        failures.append(dict(path=path, status=r.status_code))
                    if ok and writing:
                        writes += 1
                except Exception as exc:
                    status[type(exc).__name__] += 1
                    failures.append(dict(path=path, error=type(exc).__name__))
                elapsed_ms = (time.perf_counter()-t)*1000
                latencies.append(elapsed_ms)
                endpoint_times.setdefault(path, []).append(elapsed_ms)
                turn += 1
                time.sleep(a.think)
        return dict(user=identities[index]["username"], latencies=latencies, failures=failures,
                    status=dict(status), successful_writes=writes, endpoints=dict(totals), endpoint_times=endpoint_times)
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.users) as pool:
        workers = list(pool.map(worker, range(a.users)))
    elapsed = time.perf_counter()-start
    times = [x for w in workers for x in w.pop("latencies")]
    failed = sum(len(w["failures"]) for w in workers)
    endpoint_samples = {}
    for worker_result in workers:
        for path, values in worker_result.pop("endpoint_times").items():
            endpoint_samples.setdefault(path, []).extend(values)
    with db.get_conn() as c:
        after = dict(c.execute("SELECT username,COUNT(*) FROM practice_attempts GROUP BY username"))
        integrity = c.execute("PRAGMA quick_check").fetchone()[0]
    for w in workers:
        w["persisted_attempt_delta"] = after.get(w["user"], 0)-before.get(w["user"], 0)
        w["write_reconciled"] = w["persisted_attempt_delta"] == w["successful_writes"]
    result = dict(environment="isolated single uvicorn worker, 4808-question isolated copy, no LLM load",
                  users=a.users, requested_duration_s=a.duration, elapsed_s=round(elapsed, 2),
                  requests=len(times), failed=failed, failure_rate=round(failed/max(1,len(times)), 6),
                  rps=round(len(times)/elapsed,2), p50_ms=percentile(times,.5), p95_ms=percentile(times,.95),
                  p99_ms=percentile(times,.99), max_ms=round(max(times),2),
                  think_time_s=a.think, successful_writes=sum(w["successful_writes"] for w in workers),
                  by_endpoint={path:dict(requests=len(values),p50_ms=percentile(values,.5),
                        p95_ms=percentile(values,.95),p99_ms=percentile(values,.99))
                        for path,values in endpoint_samples.items()},
                  write_reconciled=all(w["write_reconciled"] for w in workers), db_integrity=integrity,
                  acceptance={"failure_rate_max": .01, "p95_ms_max": 2000,
                              "passed": failed/max(1,len(times))<=.01 and percentile(times,.95)<=2000
                                        and all(w["write_reconciled"] for w in workers)},
                  workers=workers)
    return result


def live(a, rows):
    results = []
    with httpx.Client(base_url=a.base, timeout=180) as c:
        h = login(c, rows[0])
        for course, question in [("ds-algo", "简要解释栈与队列的区别，各给一个应用场景。"),
                                 ("os", "LRU 和 FIFO 页面置换算法有什么区别？")]:
            t, first, events = time.perf_counter(), None, []
            with c.stream("POST", "/chat/stream", json=dict(message=question, course=course), headers=h) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if line.startswith("data:"):
                        e = json.loads(line[5:])
                        if e.get("type") == "token" and first is None:
                            first = round((time.perf_counter()-t)*1000,2)
                        # 不复制教材正文；仅保留引用定位与最终答复。
                        if e.get("type") == "citation":
                            e = {k:v for k,v in e.items() if k not in ("text", "snippet")}
                        if e.get("type") != "reasoning":
                            events.append(e)
            finals = [e for e in events if e.get("type") == "final"]
            results.append(dict(course=course, input=question, events=events,
                                first_token_ms=first, total_ms=round((time.perf_counter()-t)*1000,2),
                                passed=bool(finals and finals[-1].get("text")) and not any(e.get("type")=="error" for e in events)))
    mode = ("actual local GPU model application SSE; separate from fixed-length GPU benchmark"
            if a.model_scope == "local-gpu" else "actual configured external LLM; not GPU benchmark")
    return dict(environment=a.base, mode=mode, cases=results)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["functional", "load", "live"])
    p.add_argument("--base", default="http://127.0.0.1:8015/api")
    p.add_argument("--accounts", type=Path, default=ROOT / ".private/accounts.json")
    p.add_argument("--isolated", action="store_true")
    p.add_argument("--model-scope", choices=["external-api", "local-gpu"], default="external-api")
    p.add_argument("--duration", type=int, default=60)
    p.add_argument("--users", type=int, default=10)
    p.add_argument("--think", type=float, default=.1)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    rows = json.loads(a.accounts.read_text())
    result = {"captured_at": datetime.now(timezone.utc).isoformat(), **globals()[a.mode](a, rows)}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k:v for k,v in result.items() if k not in ("workers","checks","cases")}, ensure_ascii=False))
    failed = any(not x["ok"] for x in result.get("checks",[])) or any(not x["passed"] for x in result.get("cases",[]) if isinstance(x,dict))
    if a.mode == "load":
        failed = not result["acceptance"]["passed"]
    raise SystemExit(1 if failed else 0)
