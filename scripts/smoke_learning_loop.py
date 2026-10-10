"""P2/P3 闭环冒烟检查，全程在临时数据库运行，不改开发数据。"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "data" / "processed" / "courseloop.db"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="courseloop-smoke-") as tmp:
        target = Path(tmp) / "smoke.db"
        shutil.copy2(SOURCE_DB, target)
        os.environ["CL_DB_PATH"] = str(target)

        # 必须在设置环境变量后导入，确保所有模块只连接临时库。
        from backend.agents import practice_generator
        from backend.api.auth import ProfileReq, update_profile
        from backend.api.core_routes import learning_center, practice_recommend
        from backend.core import db
        from backend.core.config import settings
        from backend.learning import review_service

        settings.llm_base_url = ""
        settings.llm_api_key = ""
        settings.llm_text_base_url = ""
        settings.llm_text_api_key = ""
        db.init_db()

        username = "__smoke_loop__"
        ok, _ = db.register_user(username, "smoke-pass", "闭环检查")
        assert ok, "临时用户创建失败"
        changed = update_profile(
            ProfileReq(name="测试用户", college="信息与计算学院"),
            ident={"username": username, "role": "student"},
        )
        assert changed["user"]["name"] == "测试用户", "个人资料应可更新并回读"

        rec = practice_recommend(course="co", limit=4, per_point=2,
                                 ident={"username": username, "role": "student"})
        assert rec["weak_points"] and rec["items"], "冷启动推荐应有知识点和题目"
        assert rec["cold_start"] is True, "新用户应进入冷启动"
        bank = next(item for item in rec["items"] if item["source"] == "bank")
        db.save_attempt(username, "smoke", 100, problem_id=int(bank["problem_id"]))

        start = date.today()
        plan = review_service.create_plan(
            username, "co", start_date=start.isoformat(),
            exam_date=(start + timedelta(days=30)).isoformat(), daily_minutes=30,
            max_points=6,
        )
        assert plan["tasks"], "复习计划应有任务"
        task = plan["tasks"][0]
        try:
            review_service.manual_checkin(username, int(task["id"]))
        except ValueError:
            pass
        else:
            raise AssertionError("练习任务不能绕过判题手工完成")

        first = review_service.record_attempt(
            username, int(task["id"]), int(task["knowledge_id"]), True, problem_id=1)
        duplicate = review_service.record_attempt(
            username, int(task["id"]), int(task["knowledge_id"]), True, problem_id=1)
        assert first and duplicate and first["completed_count"] == duplicate["completed_count"], \
            "同一道题重复提交不能重复累计"
        updated = duplicate
        for problem_id in range(2, int(task["target_count"]) + 1):
            updated = review_service.record_attempt(
                username, int(task["id"]), int(task["knowledge_id"]), True,
                problem_id=problem_id)
        assert updated and updated["completed"], "达到目标题数后任务应完成"
        active = review_service.get_active(username, "co")
        assert active and active["progress"]["completed_tasks"] == 1, "进度应持久化"
        center = learning_center(course="co", ident={"username": username, "role": "student"})
        assert center["overview"]["attempts"] == 1 and center["plan"], \
            "学习中心应聚合当前课程作答与活动计划"

        sample = {
            "verified": True, "stem": "[smoke] 2+2 等于多少？", "difficulty": 1,
            "type": "数值题", "answer": "4",
            "final_answer_canonical": {"kind": "int", "value": 4},
        }
        pid = practice_generator.persist(sample, "co", plan["tasks"][0]["name"])
        assert (pid and practice_generator.persist(
            sample, "co", plan["tasks"][0]["name"]) == pid), "验证题应写入且保持幂等"
        assert practice_generator.persist({**sample, "verified": False, "stem": "reject"},
                                            "co", plan["tasks"][0]["name"]) == 0

        print("✓ 推荐 → 计划 → 判题回写 → 生成题持久化闭环通过（临时数据库）")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
