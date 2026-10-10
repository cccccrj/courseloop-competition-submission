"""只在隔离库剖析业务函数与SQLite查询计划，不发模型请求，不输出业务正文。"""
import argparse
import cProfile
import json
import os
import pstats
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--include-write", action="store_true", help="另测合成写入；默认只读以保持前后记录数一致")
    args = parser.parse_args()
    if not ROOT.name.startswith("courseloop-submission-test-"):
        raise SystemExit("只允许隔离测试环境测试目录")
    from backend.core import db
    from backend.api import core_routes as routes
    identity = {"username": "competition-load-00", "role": "student"}
    result = {"scope": "isolated direct service CPU/SQLite profile; not HTTP capacity", "functions": {}}
    calls = [("learning_center", lambda: routes.learning_center("os", identity)),
             ("practice_recommend", lambda: routes.practice_recommend("os", ident=identity))]
    if args.include_write:
        from scripts.submission_test import gold_problem
        p = gold_problem()
        full = db.get_problem(p["id"], with_solution=True)
        calls.append(("writeback", lambda: routes._writeback("competition-profile-only", full,
                     {"passed":True}, "", "", problem_id=p["id"], answer_text=p["value"])))
    result["includes_synthetic_writes"] = args.include_write
    for name, call in calls:
        profile = cProfile.Profile()
        samples = []
        profile.enable()
        for _ in range(args.iterations):
            start = time.perf_counter()
            call()
            samples.append((time.perf_counter()-start)*1000)
        profile.disable()
        stats = pstats.Stats(profile)
        functions = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:25]
        result["functions"][name] = dict(mean_ms=sum(samples)/len(samples), min_ms=min(samples),
                max_ms=max(samples), iterations=len(samples), cumulative=[dict(
                    function=f"{Path(key[0]).name}:{key[1]}:{key[2]}", calls=value[1],
                    self_s=value[2], cumulative_s=value[3]) for key,value in functions])
    with db.get_conn() as conn:
        result["counts"] = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                            for table in ("problems", "practice_attempts", "knowledge_points")}
        result["knowledge_stats_query_plan"] = [dict(row) for row in conn.execute(
            "EXPLAIN QUERY PLAN " + db._KNOWLEDGE_STATS_SQL, {"u":identity["username"],"course":"os"})]
        result["indexes"] = [dict(row) for row in conn.execute(
            "SELECT name,tbl_name FROM sqlite_master WHERE type='index' ORDER BY name")]
    result["loadavg"] = list(os.getloadavg())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k:round(v["mean_ms"],2) for k,v in result["functions"].items()}))

if __name__ == "__main__":
    main()
