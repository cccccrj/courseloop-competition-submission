"""课程从代码搬进数据库：读写往返、缓存、管理接口与护栏。

这一组压两类风险：

1. **搬家丢东西**。四门课的知识点树是按 2026 官方大纲校准过的，序列化往返
   必须逐字段一致 —— 丢一个章节不会报错，只会让那一章的题永远推荐不出来。
2. **改大纲让已有数据悬空**。改 knowledge_taxonomy 之后，库里已有的
   knowledge_points 可能对不上任何章节。这同样不报错，只会让掌握度聚合到
   一堆孤儿节点上，所以每次写课程都跟一次审计并如实返回条数。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.core import courses as courses_mod
from backend.core import db
from backend.core.config import settings
from backend.core.courses import DEFAULT_SEED, Course
from backend.core.security import issue_token


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    courses_mod.invalidate_cache()
    db.init_db()
    yield tmp_path
    courses_mod.invalidate_cache()


@pytest.fixture()
def admin_client(fresh_db):
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET role='admin' WHERE username='demo_ta'")
    return TestClient(app), issue_token("demo_ta", "admin")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# 1. 序列化往返
# --------------------------------------------------------------------------
def test_seeded_courses_round_trip_field_by_field(fresh_db):
    """播种进库再读出来，必须与 DEFAULT_SEED 的字面量逐字段一致。"""
    loaded = courses_mod.all_courses()
    assert list(loaded) == [c.id for c in DEFAULT_SEED], "顺序要按 sort_order 保持登记序"
    for seed in DEFAULT_SEED:
        got = loaded[seed.id]
        assert got == seed, f"{seed.id} 往返后与字面量不一致"


def test_topics_survive_the_round_trip(fresh_db):
    """知识点树是最容易在序列化里丢东西的字段，单独压一次。"""
    for seed in DEFAULT_SEED:
        got = courses_mod.get_course(seed.id)
        assert got.topics == seed.topics
        assert len(got.knowledge_taxonomy) == len(seed.knowledge_taxonomy)


def test_falls_back_to_seed_when_table_missing(tmp_path, monkeypatch):
    """改造前的旧库没有 courses 表 —— 必须回退字面量，而不是让讲解与组卷崩掉。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "legacy.db"))
    courses_mod.invalidate_cache()
    with db.get_conn() as conn:                      # 建库但不建 courses 表
        conn.execute("CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY)")
    assert courses_mod.course_ids() == [c.id for c in DEFAULT_SEED]
    courses_mod.invalidate_cache()


# --------------------------------------------------------------------------
# 2. 缓存
# --------------------------------------------------------------------------
def test_write_invalidates_cache_in_process(fresh_db):
    assert courses_mod.get_course("os").short == "操作系统"
    changed = Course(**{**courses_mod.get_course("os").__dict__, "short": "OS原理"})
    courses_mod.upsert_course(changed)
    assert courses_mod.get_course("os").short == "OS原理", "写完没有立即生效"


def test_cache_is_keyed_per_database(tmp_path, monkeypatch):
    """两个库各自缓存 —— 与 retriever 的引擎缓存同一个理由。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "a.db"))
    courses_mod.invalidate_cache()
    db.init_db()
    courses_mod.upsert_course(Course(id="onlya", display="《只在A》", short="只在A",
                                     exam="", score=10, code_enabled=False,
                                     verification_types=["rule"],
                                     knowledge_taxonomy=[{"chapter": "章", "topics": ["点"]}]))
    assert "onlya" in courses_mod.course_ids()

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "b.db"))
    db.init_db()
    assert "onlya" not in courses_mod.course_ids(), "B 库读到了 A 库的课程"
    courses_mod.invalidate_cache()


# --------------------------------------------------------------------------
# 3. 自助开课（这一阶段的目的本身）
# --------------------------------------------------------------------------
def test_admin_can_open_a_brand_new_course(admin_client):
    client, token = admin_client
    body = {
        "id": "linear-algebra", "display": "《线性代数》", "short": "线代",
        "exam": "考研数学一", "score": 34, "code_enabled": False,
        "verification_types": ["numeric", "rule", "citation"],
        "knowledge_taxonomy": [
            {"chapter": "行列式", "topics": ["行列式定义", "克拉默法则"]},
            {"chapter": "矩阵", "topics": ["矩阵运算", "逆矩阵", "初等变换"]},
        ],
        "error_examples": {"calculation": ["行列式按行展开符号错"]},
        "sample_questions": ["三阶行列式怎么算？"],
        "answer_format": "本门为非代码题。",
    }
    r = client.post("/admin/courses", json=body, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["knowledge_audit"]["existing_knowledge_points"] == 0

    # 学生侧的 /courses 立刻能看到，且字段完整
    listed = client.get("/courses").json()
    new = [c for c in listed["items"] if c["id"] == "linear-algebra"]
    assert new and new[0]["short"] == "线代"
    assert new[0]["topics"] == ["行列式定义", "克拉默法则", "矩阵运算", "逆矩阵", "初等变换"]

    # 全站的课程校验也认它
    from backend.core.courses import get_course, is_valid_course
    assert is_valid_course("linear-algebra")
    assert get_course("linear-algebra").display == "《线性代数》"


def test_new_course_flows_into_downstream_modules(admin_client):
    """新课不是只出现在课表里 —— 组卷蓝图与预算分摊都要认它。"""
    client, token = admin_client
    from backend.learning import mock_exam, study_budget

    before = study_budget.course_share("os")
    client.post("/admin/courses", json={
        "id": "math", "display": "《高等数学》", "short": "高数", "score": 50,
        "verification_types": ["numeric", "rule"],
        "knowledge_taxonomy": [{"chapter": "极限", "topics": ["数列极限", "函数极限"]}],
    }, headers=_auth(token))

    ids = [b["course"] for b in mock_exam.blueprint()]
    assert "math" in ids, "新课没进组卷蓝图"
    assert study_budget.course_share("os") < before, "新课加入后旧课的预算占比应被稀释"


def test_standard_score_follows_the_course_table(admin_client):
    """卷面标准分要跟着课表走，否则加一门课就会让所有人的折算分系统性偏低。

    原先它是写死的 `40×2+70=150`，在 408 四门下恰好等于 45+45+35+25，
    两种算法看不出差别；加一门 40 分的课之后真实卷面是 190，而常量仍报 150。
    """
    client, token = admin_client
    from backend.learning import mock_exam

    assert mock_exam.standard_score() == 150, "408 四门下必须与原常量逐位一致"
    client.post("/admin/courses", json={
        "id": "extra", "display": "《附加课》", "short": "附加", "score": 40,
        "verification_types": ["rule"],
        "knowledge_taxonomy": [{"chapter": "章", "topics": ["点"]}],
    }, headers=_auth(token))
    assert mock_exam.standard_score() == 190
    # 蓝图的卷面分之和必须等于标准分 —— 这正是原来对不上的那个等式
    assert sum(b["score"] for b in mock_exam.blueprint()) == mock_exam.standard_score()


def test_disable_is_soft_and_keeps_data(admin_client):
    client, token = admin_client
    r = client.delete("/admin/courses/net", headers=_auth(token))
    assert r.status_code == 200 and r.json()["disabled"] is True
    assert "net" not in courses_mod.course_ids(), "停用后不该再出现在课表里"
    # 但管理页仍看得到，且能重新启用
    admin_items = client.get("/admin/courses", headers=_auth(token)).json()["items"]
    net = [c for c in admin_items if c["id"] == "net"][0]
    assert net["enabled"] is False
    assert net["knowledge_taxonomy"], "停用不该抹掉课程定义"


# --------------------------------------------------------------------------
# 4. 护栏
# --------------------------------------------------------------------------
def test_unknown_verification_type_is_refused(admin_client):
    """上一轮踩过的坑：verification_type='choice' 会静默降级成关键词判分。"""
    client, token = admin_client
    r = client.post("/admin/courses", json={
        "id": "bad", "display": "《坏课》", "short": "坏课",
        "verification_types": ["choice"],
        "knowledge_taxonomy": [{"chapter": "章", "topics": ["点"]}],
    }, headers=_auth(token))
    assert r.status_code == 400
    assert "choice" in r.json()["detail"]


def test_code_course_must_have_unit_test(admin_client):
    client, token = admin_client
    r = client.post("/admin/courses", json={
        "id": "codey", "display": "《编程》", "short": "编程", "code_enabled": True,
        "verification_types": ["rule"],
        "knowledge_taxonomy": [{"chapter": "章", "topics": ["点"]}],
    }, headers=_auth(token))
    assert r.status_code == 400 and "unit_test" in r.json()["detail"]


def test_empty_chapter_or_topics_is_refused(admin_client):
    client, token = admin_client
    for taxonomy in ([{"chapter": "", "topics": ["点"]}], [{"chapter": "章", "topics": []}]):
        r = client.post("/admin/courses", json={
            "id": "x1", "display": "《X》", "short": "X",
            "verification_types": ["rule"], "knowledge_taxonomy": taxonomy,
        }, headers=_auth(token))
        assert r.status_code == 400


def test_taxonomy_change_reports_orphaned_knowledge_points(admin_client):
    """改大纲让已有知识点悬空时，必须如实报数而不是静默吞掉。"""
    client, token = admin_client
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO knowledge_points(course_id,chapter,name,description,difficulty)"
            " VALUES('os',?,?,'',2)",
            [("进程管理", "进程与线程"), ("内存管理", "页面置换算法"),
             ("某个已删章节", "量子隧穿效应")])

    os_course = courses_mod.get_course("os")
    body = {
        "id": "os", "display": os_course.display, "short": os_course.short,
        "exam": os_course.exam, "score": os_course.score,
        "verification_types": os_course.verification_types,
        # 大纲砍到只剩一章
        "knowledge_taxonomy": [{"chapter": "进程管理", "topics": ["进程与线程"]}],
        "error_examples": os_course.error_examples,
        "sample_questions": os_course.sample_questions,
        "answer_format": os_course.answer_format,
    }
    r = client.put("/admin/courses/os", json=body, headers=_auth(token))
    assert r.status_code == 200, r.text
    audit = r.json()["knowledge_audit"]
    assert audit["existing_knowledge_points"] == 3
    assert audit["unclassified"] >= 1, "砍掉章节后应报出对不上的知识点"
    assert audit["unclassified_samples"], "要给出具体是哪些，而不是只报个数字"


# --------------------------------------------------------------------------
# 5. 权限
# --------------------------------------------------------------------------
@pytest.mark.parametrize("role", ["student", "ta", "teacher"])
def test_non_admin_cannot_touch_admin_routes(fresh_db, role):
    client = TestClient(app)
    token = issue_token("someone", role)
    assert client.get("/admin/courses", headers=_auth(token)).status_code == 403
    assert client.delete("/admin/courses/net", headers=_auth(token)).status_code == 403


def test_anonymous_cannot_touch_admin_routes(fresh_db):
    assert TestClient(app).get("/admin/courses").status_code == 403


def test_public_register_still_cannot_create_admin(fresh_db):
    """公网注册一律 student —— 这条防提权不打折。"""
    client = TestClient(app)
    r = client.post("/auth/register", json={"username": "sneaky", "password": "pw123456",
                                            "role": "admin"})
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "student"


# --------------------------------------------------------------------------
# 6. 试卷结构改成可配置
# --------------------------------------------------------------------------
def test_exam_structure_is_configurable(admin_client):
    client, token = admin_client
    from backend.core.courses import EXAM_STRUCTURE

    assert courses_mod.exam_structure() == EXAM_STRUCTURE, "没配过就回退原常量"
    r = client.put("/admin/settings", json={"exam_structure": "闭卷 100 分 / 120 分钟"},
                   headers=_auth(token))
    assert r.status_code == 200
    assert courses_mod.exam_structure() == "闭卷 100 分 / 120 分钟"
    assert client.get("/courses").json()["exam_structure"] == "闭卷 100 分 / 120 分钟"
