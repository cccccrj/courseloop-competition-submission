"""CPU 算法回归：数值判分、代码基准、掌握度和 FTS5；不冒充 LLM/GPU 实验。"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core import db
from backend.rag.retriever import get_engine
from backend.verification.numeric_checker import check_numeric, canonical_text
from backend.verification.unit_test_runner import run_python_tests
from backend.learning.mastery import update_mastery, apply_forgetting
from scripts.seed_problems import PROBLEMS


def run():
    result = {"scope": "CPU deterministic algorithm regression, not educational efficacy"}
    codes = []
    for p in PROBLEMS:
        if p.get("code"):
            t = time.perf_counter()
            r = run_python_tests(p["code"], p["func"], p["tests"])
            codes.append(dict(function=p["func"], passed=r.passed, test_cases=len(p["tests"]),
                              ms=round((time.perf_counter()-t)*1000,2)))
    result["original_code_baseline"] = dict(total=len(codes), passed=sum(x["passed"] is True for x in codes), cases=codes)
    with db.get_conn() as c:
        rows = c.execute("SELECT id,answer FROM problems WHERE verification_type='numeric' ORDER BY id").fetchall()
    numeric = []
    for row in rows:
        try:
            canonical = json.loads(row["answer"])
        except (ValueError, TypeError):
            continue
        if not isinstance(canonical, dict):
            continue
        r = check_numeric(canonical_text(canonical), canonical)
        numeric.append(dict(problem_id=row["id"], kind=canonical.get("kind"), passed=r.passed))
    result["canonical_self_consistency"] = dict(total=len(numeric), passed=sum(x["passed"] is True for x in numeric),
                                                undetermined=sum(x["passed"] is None for x in numeric),
                                                failed=sum(x["passed"] is False for x in numeric),
                                                caveat="uses stored gold; measures representation consistency, NOT gold correctness or model accuracy",
                                                exceptions=[x for x in numeric if x["passed"] is not True])
    checks = [update_mastery(.5, True)>.5, update_mastery(.5, False)<.5,
              update_mastery(.5, True, hints_used=3)<update_mastery(.5, True),
              apply_forgetting(.8,14)<.8]
    result["mastery_invariants"] = dict(total=len(checks), passed=sum(checks))
    queries = [("ds-algo", "栈与队列"), ("co", "Cache"), ("os", "LRU"), ("net", "TCP 三次握手")]
    engine = get_engine()
    hits = []
    for course, query in queries:
        t = time.perf_counter()
        found = engine.retrieve(query, course=course, top_n=3, doc_types=["textbook","courseware"])
        hits.append(dict(course=course, query=query, count=len(found), ms=round((time.perf_counter()-t)*1000,2),
                         all_course_matched=all(x.course==course for x in found),
                         sources=[dict(source=x.source,title_path=x.title_path,page_start=x.page_start,page_end=x.page_end)
                                  for x in found]))
    result["retrieval_smoke"] = dict(total=4, nonempty=sum(x["count"]>0 for x in hits), cases=hits,
                                     caveat="4-query smoke, NOT independent retrieval relevance accuracy")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    result = run()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k:{n:v for n,v in entry.items() if n not in ("cases","exceptions")}
                      for k,entry in result.items() if isinstance(entry,dict)},ensure_ascii=False))
