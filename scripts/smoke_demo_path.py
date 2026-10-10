"""演示路径端到端冒烟：学生提交 → 提示 → 首错 → 重讲 → 变式 → 复测 → 记录 → 重练
→ 复习 → 请助教复核 → 助教改判 → 学生看到结论。外加四种边界：中途刷新、
重复提交复核、AI 未开通、拍照未开通。

起因见 docs/34 §3.7：12 步演示路径里第 6-7 步（变式 → 复测）和第 11-12 步（依据
不足 → 助教复核）此前走不通，而 smoke_learning_loop.py 只覆盖后端函数链、不走接口。
这个脚本走 HTTP 接口，跟前端调的是同一组端点。

全程在**临时复制的库**上跑，不碰开发数据。默认把 LLM 换成桩（结果可复现、不花钱）；
加 --live 则用 .env 里配置的真实模型跑一遍（演示前最后确认用）。

    python -m scripts.smoke_demo_path            # 桩
    python -m scripts.smoke_demo_path --live     # 真实模型
    python -m scripts.smoke_demo_path --out report.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "data" / "processed" / "courseloop.db"

PROBLEM = "某系统采用 LRU 页面置换，分配 3 个页框，访问序列 1 2 3 4 1 2 5 1 2 3 4 5，缺页几次？"
WRONG = "我认为缺页 8 次：前三次都缺页，然后 4 替换 1……"
STUB_SOLUTION = {"knowledge_points": ["页面置换算法"], "strategy": "按访问序列逐个模拟页框",
                 "steps": [{"index": 1, "action": "列出访问序列并逐个模拟", "basis": "LRU 定义"}],
                 "final_answer": "缺页 10 次"}
STUB_VARIANT = {"stem": "FIFO 置换，3 个页框，访问序列 1 2 3 4 1 2 5，缺页几次？",
                "answer": "缺页 7 次", "hint": "逐个模拟页框", "verified": True}


def _install_stubs(tutor) -> None:
    tutor.solver_mod.solve = lambda *a, **k: dict(STUB_SOLUTION)
    tutor.pg_mod.generate = lambda *a, **k: dict(STUB_VARIANT)

    def diagnose(problem, answer, reference="", **kw):
        right = answer.strip() == (reference or "").strip()
        if right:
            return {"correct": True, "score": 100, "error_tags": [], "diagnosis": ""}
        return {"correct": False, "score": 0, "error_tags": ["concept_confuse"],
                "error_tag_names": ["概念混淆"], "first_wrong_step": 2,
                "diagnosis": "第 2 步把 LRU 当成了 FIFO：替换的应是**最久未使用**的页。",
                "reexplain": "换个讲法：把页框想成一摞书，每用一次就放到最上面，淘汰最底下那本。",
                "remediation": "先对比 LRU 与 FIFO，再做一道辨析题。"}

    tutor.diag_mod.diagnose = diagnose


def main() -> int:
    ap = argparse.ArgumentParser(description="演示路径端到端冒烟（临时库）")
    ap.add_argument("--live", action="store_true", help="用真实模型，不装桩")
    ap.add_argument("--out", default="", help="把逐步结果写成 JSON")
    args = ap.parse_args()

    results: list[dict] = []

    def step(name: str, fn):
        t0 = time.time()
        try:
            detail = fn() or ""
            results.append({"step": name, "ok": True, "detail": detail, "ms": int((time.time() - t0) * 1000)})
            print(f"✓ {name}  {detail}")
            return True
        except Exception as exc:  # noqa: BLE001
            results.append({"step": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}",
                            "ms": int((time.time() - t0) * 1000)})
            print(f"✗ {name}  {type(exc).__name__}: {exc}")
            return False

    with tempfile.TemporaryDirectory(prefix="courseloop-demo-") as tmp:
        target = Path(tmp) / "demo.db"
        shutil.copy2(SOURCE_DB, target)
        os.environ["CL_DB_PATH"] = str(target)
        os.environ["CL_LOGS_DIR"] = str(Path(tmp) / "logs")

        from fastapi.testclient import TestClient
        from backend.agents import tutor_graph as tutor
        from backend.api.main import app
        from backend.core import db
        from backend.core.config import settings
        from backend.core.security import issue_token

        db.init_db()
        if not args.live:
            _install_stubs(tutor)
        c = TestClient(app, raise_server_exceptions=False)
        stu_name, course = "__smoke_demo__", "os"
        db.register_user(stu_name, "smoke-pass", "演示冒烟")
        stu = {"Authorization": f"Bearer {issue_token(stu_name, 'student')}"}
        ta = {"Authorization": f"Bearer {issue_token('demo_ta', 'ta')}"}
        ctx: dict = {}

        def s1():
            r = c.post("/tutor/start", json={"problem": PROBLEM, "course": course}, headers=stu).json()
            ctx["sid"] = r["session_id"]
            return "会话已建立"

        def s2():
            h = c.post("/tutor/hint", json={"session_id": ctx["sid"]}, headers=stu).json()["hint"]
            assert h["level"] == 1 and "答案" not in h["title"], "第一级提示不应直接给答案"
            return f"提示 L{h['level']}：{h['title']}"

        def s3_5():
            r = c.post("/tutor/submit", json={"session_id": ctx["sid"], "student_answer": WRONG}, headers=stu).json()
            assert r["correct"] is False and r["first_wrong_step"], "应定位首错"
            assert r["error_tags"], "应给出错因"
            ctx["rid"] = r.get("error_record_id")
            assert ctx["rid"], "错题应落库并返回记录号"
            return f"首错第 {r['first_wrong_step']} 步 · 错因 {r['error_tags'][0]} · 重讲 {'有' if r.get('reexplain') else '无'}"

        def s6():
            r = c.post("/tutor/variant", json={"session_id": ctx["sid"]}, headers=stu).json()
            assert r.get("current_problem"), r.get("error") or "变式题生成失败"
            ctx["variant_answer"] = (r["problem"].get("answer") or "")
            return "会话已切到变式题"

        def s7():
            ans = ctx["variant_answer"] if not args.live else ctx["variant_answer"] or "7"
            r = c.post("/tutor/submit", json={"session_id": ctx["sid"], "student_answer": ans}, headers=stu).json()
            if not args.live:
                assert r["correct"] is True, "按变式题的答案作答应判对（判的必须是变式题）"
            return f"复测判定：{'正确' if r.get('correct') else '仍有错'}"

        def s8():
            items = c.get(f"/records/errors?course={course}", headers=stu).json()["items"]
            mine = next(e for e in items if e["id"] == ctx["rid"])
            assert mine["stem"], "错题本应带题干"
            ctx["kid"] = mine["knowledge_id"]
            return f"错题本 {len(items)} 条"

        def s9():
            assert ctx["kid"], "错题应关联知识点，「去重练」才有落点"
            r = c.get(f"/practice/recommend?course={course}&limit=4", headers=stu)
            assert r.status_code == 200
            return "个性化练习可按知识点进入"

        def s10():
            r = c.get(f"/learning/center?course={course}", headers=stu).json()
            assert any(p["knowledge_id"] == ctx["kid"] for p in r["weak_points"]) or r["overview"]["attempts"], \
                "学习中心应反映这次作答"
            return f"学习中心已更新（作答 {r['overview']['attempts']} 次）"

        def s11():
            r = c.post("/review-requests", json={"error_record_id": ctx["rid"]}, headers=stu)
            assert r.status_code == 200, r.text
            today = c.get(f"/teaching/today?course={course}", headers=ta).json()
            assert today["primary"]["kind"] == "review", "助教今日首屏应置顶学生申请"
            q = c.get(f"/teaching/errors?reviewed=no&course={course}", headers=ta).json()["items"]
            top = q[0]
            assert top["id"] == ctx["rid"] and top["problem_stem"] and top["student_answer"], \
                "复核队列置顶且带题目与作答"
            return f"助教首屏：{today['primary']['title']}"

        def s12():
            r = c.post("/teaching/review-error", json={
                "record_id": ctx["rid"], "verdict": "override", "error_type": "calculation",
                "note": "其实是模拟时数错了"}, headers=ta).json()
            assert r["impact"], "改判应说明对下游的影响"
            mine = next(e for e in c.get(f"/records/errors?course={course}", headers=stu).json()["items"]
                        if e["id"] == ctx["rid"])
            assert mine["ta_reviewed"] and mine["ta_note"], "学生应看到助教结论"
            return "；".join(r["impact"])

        def b_refresh():
            tutor._SESSIONS.clear()   # 等同后端重启 / 前端刷新后从库里接回
            r = c.post("/tutor/resume", json={"course": course}, headers=stu).json()
            assert r.get("session_id") == ctx["sid"], "刷新后应接回同一会话"
            return "刷新后会话接回"

        def b_duplicate():
            r1 = c.post("/review-requests", json={"error_record_id": ctx["rid"]}, headers=stu)
            r2 = c.post("/review-requests", json={"error_record_id": ctx["rid"]}, headers=stu)
            assert r1.status_code == r2.status_code == 200
            n = sum(1 for e in c.get("/teaching/errors?reviewed=no", headers=ta).json()["items"]
                    if e["id"] == ctx["rid"])
            assert n == 1, "重复申请不应在队列里出现两条"
            return "重复申请只占一条"

        def b_no_llm():
            saved = (settings.llm_base_url, settings.llm_text_base_url)
            settings.llm_base_url = settings.llm_text_base_url = ""
            try:
                with c.stream("POST", "/chat/stream", json={"message": "页表在哪", "course": course},
                              headers=stu) as r:
                    text = "".join(r.iter_lines())
            finally:
                settings.llm_base_url, settings.llm_text_base_url = saved
            for bad in ("CL_", ".env", "Traceback"):
                assert bad not in text, f"AI 未开通时泄露了 {bad}"
            return "AI 未开通：业务话术、无配置名"

        def b_no_vision():
            saved = settings.llm_vision_model
            settings.llm_vision_model = ""
            try:
                png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
                       "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
                r = c.post("/grade/photo/stream", json={"image": png, "course": course}, headers=stu)
            finally:
                settings.llm_vision_model = saved
            assert r.status_code == 503 and "CL_" not in r.text, r.text
            return r.json()["detail"]

        for name, fn in [
            ("1 学生提交题目", s1), ("2 分级提示（不直接给答案）", s2),
            ("3-5 提交错误步骤 → 首错 + 错因 + 重讲", s3_5), ("6 生成变式题", s6),
            ("7 复测按变式题判分", s7), ("8 写入错题与学习记录", s8),
            ("9 可去重练", s9), ("10 学习中心随结果更新", s10),
            ("11 请助教复核 → 助教首屏置顶", s11), ("12 助教改判 → 学生看到结论", s12),
            ("边界：中途刷新", b_refresh), ("边界：重复提交复核", b_duplicate),
            ("边界：AI 未开通", b_no_llm), ("边界：拍照未开通", b_no_vision),
        ]:
            step(name, fn)

    ok = all(r["ok"] for r in results)
    print(f"\n{'全部通过' if ok else '有失败'}：{sum(r['ok'] for r in results)}/{len(results)}"
          f"（{'真实模型' if args.live else '桩'}，临时库）")
    if args.out:
        Path(args.out).write_text(json.dumps({"live": args.live, "ok": ok, "steps": results},
                                             ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
