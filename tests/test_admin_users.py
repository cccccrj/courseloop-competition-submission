"""用户管理与停用语义。

压的两条主线：

1. **停用要真的停得掉**。令牌有效期 30 天，只在登录时挡的话，被停用的人还能
   用旧令牌继续用一个月 —— 而停用通常是安全动作（人离校了、号被人拿到了）。
2. **提权路径只有一条**。公网注册永远只能建 student；管理员只能由管理员或
   能登上机器的人创建；最后一个管理员不能把自己锁在门外。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.core import courses as courses_mod
from backend.core import db
from backend.core.config import settings
from backend.core.security import issue_token


@pytest.fixture()
def admin_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    courses_mod.invalidate_cache()
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET role='admin' WHERE username='demo_ta'")
    yield TestClient(app), issue_token("demo_ta", "admin"), tmp_path
    courses_mod.invalidate_cache()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# 1. 建号与角色
# --------------------------------------------------------------------------
def test_admin_can_create_users_of_every_role(admin_client):
    client, token, _ = admin_client
    for role in ("student", "ta", "teacher", "admin"):
        r = client.post("/admin/users", json={
            "username": f"u_{role}", "password": "pw12345678", "name": role, "role": role,
        }, headers=_auth(token))
        assert r.status_code == 200, r.text
        assert r.json()["user"]["role"] == role, "角色必须真的落库，不能静默降级"


def test_created_admin_can_actually_use_admin_routes(admin_client):
    """建出来的管理员要真的能用 —— 上一轮的 bug 正是建成了打 403 的假管理员。"""
    client, token, _ = admin_client
    client.post("/admin/users", json={"username": "newadmin", "password": "pw12345678",
                                      "role": "admin"}, headers=_auth(token))
    r = client.post("/auth/login", json={"username": "newadmin", "password": "pw12345678"})
    assert r.status_code == 200
    assert client.get("/admin/users", headers=_auth(r.json()["token"])).status_code == 200


def test_unknown_role_is_refused_not_downgraded(admin_client):
    client, token, _ = admin_client
    r = client.post("/admin/users", json={"username": "xx", "password": "pw12345678",
                                          "role": "superuser"}, headers=_auth(token))
    assert r.status_code == 400 and "superuser" in r.json()["detail"]


def test_admin_password_floor_is_higher_than_public_register(admin_client):
    client, token, _ = admin_client
    r = client.post("/admin/users", json={"username": "shortpw", "password": "pw1234"},
                    headers=_auth(token))
    assert r.status_code == 422, "管理页建号口令下限 8 位，比公网注册的 6 位更严"


# --------------------------------------------------------------------------
# 2. 停用
# --------------------------------------------------------------------------
def test_disabled_user_cannot_login(admin_client):
    client, token, _ = admin_client
    client.post("/admin/users", json={"username": "leaver", "password": "pw12345678"},
                headers=_auth(token))
    assert client.post("/auth/login", json={"username": "leaver",
                                            "password": "pw12345678"}).status_code == 200
    client.put("/admin/users/leaver", json={"status": "disabled"}, headers=_auth(token))
    assert client.post("/auth/login", json={"username": "leaver",
                                            "password": "pw12345678"}).status_code == 401


def test_disabled_user_existing_token_stops_working_immediately(admin_client):
    """这条是本组最要紧的一条：停用不能等 30 天令牌过期才生效。"""
    client, token, _ = admin_client
    client.post("/admin/users", json={"username": "compromised", "password": "pw12345678"},
                headers=_auth(token))
    victim = client.post("/auth/login", json={"username": "compromised",
                                              "password": "pw12345678"}).json()["token"]
    assert client.get("/records/errors", headers=_auth(victim)).status_code == 200

    client.put("/admin/users/compromised", json={"status": "disabled"}, headers=_auth(token))
    r = client.get("/records/errors", headers=_auth(victim))
    assert r.status_code == 401, "旧令牌在停用后必须立刻失效"
    assert "停用" in r.json()["detail"], "要说清是被停用，而不是笼统的登录失效"


def test_disabled_user_cannot_re_enable_via_profile_update(admin_client):
    """status 刻意不在 _PROFILE_FIELDS 里 —— 否则改一次资料就能自己解封。"""
    client, token, _ = admin_client
    client.post("/admin/users", json={"username": "sneaky2", "password": "pw12345678"},
                headers=_auth(token))
    victim = client.post("/auth/login", json={"username": "sneaky2",
                                              "password": "pw12345678"}).json()["token"]
    client.put("/admin/users/sneaky2", json={"status": "disabled"}, headers=_auth(token))
    r = client.put("/auth/profile", json={"name": "我又回来了", "status": "active"},
                   headers=_auth(victim))
    assert r.status_code == 401
    assert db.user_status("sneaky2") == "disabled"


def test_re_enabling_restores_access(admin_client):
    client, token, _ = admin_client
    client.post("/admin/users", json={"username": "backagain", "password": "pw12345678"},
                headers=_auth(token))
    client.put("/admin/users/backagain", json={"status": "disabled"}, headers=_auth(token))
    client.put("/admin/users/backagain", json={"status": "active"}, headers=_auth(token))
    assert client.post("/auth/login", json={"username": "backagain",
                                            "password": "pw12345678"}).status_code == 200


def test_old_rows_without_status_column_are_treated_as_active(tmp_path, monkeypatch):
    """老库里 status 列还不存在或为 NULL —— 一律当 active。

    默认 disabled 的话，迁移时机没对上就会把全体用户锁在门外，
    而现场表现是"所有人都登不进去"。
    """
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "old.db"))
    courses_mod.invalidate_cache()
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET status=NULL")
    assert db.user_status("demo_student") == "active"
    courses_mod.invalidate_cache()


# --------------------------------------------------------------------------
# 3. 别把自己锁在门外
# --------------------------------------------------------------------------
def test_admin_cannot_disable_or_demote_self(admin_client):
    client, token, _ = admin_client
    assert client.put("/admin/users/demo_ta", json={"status": "disabled"},
                      headers=_auth(token)).status_code == 400
    assert client.put("/admin/users/demo_ta", json={"role": "student"},
                      headers=_auth(token)).status_code == 400


def test_two_admins_can_demote_each_other(admin_client):
    client, token, _ = admin_client
    client.post("/admin/users", json={"username": "admin2", "password": "pw12345678",
                                      "role": "admin"}, headers=_auth(token))
    assert client.put("/admin/users/admin2", json={"role": "ta"},
                      headers=_auth(token)).status_code == 200


def test_last_active_admin_cannot_be_demoted_by_a_stale_token(admin_client):
    """「至少保留一个管理员」这条兜底真正会触发的场景。

    角色变更要重新登录才生效（security.py 的既有取舍），所以存在一个窗口：
    某人的库内角色已经不是 admin 了，手里的旧令牌却还写着 admin。这段时间里
    他若把最后一个真管理员降级，就再没有任何接口能把权限拿回来 ——
    只能登上机器跑 scripts/manage_users。自我降级那条守卫拦不住这种情况
    （降的不是自己），所以需要这条兜底。
    """
    client, token, _ = admin_client
    client.post("/admin/users", json={"username": "admin2", "password": "pw12345678",
                                      "role": "admin"}, headers=_auth(token))
    stale = client.post("/auth/login", json={"username": "admin2",
                                             "password": "pw12345678"}).json()["token"]
    # 库里把 admin2 降级，但它手上的令牌还写着 admin
    db.set_user_role("admin2", "student")
    r = client.put("/admin/users/demo_ta", json={"role": "student"}, headers=_auth(stale))
    assert r.status_code == 400 and "至少" in r.json()["detail"]


def test_update_unknown_user_is_404(admin_client):
    client, token, _ = admin_client
    assert client.put("/admin/users/nobody", json={"status": "disabled"},
                      headers=_auth(token)).status_code == 404


# --------------------------------------------------------------------------
# 4. 列表不泄露口令材料
# --------------------------------------------------------------------------
def test_user_list_never_returns_password_material(admin_client):
    client, token, _ = admin_client
    items = client.get("/admin/users", headers=_auth(token)).json()["items"]
    assert items
    for u in items:
        assert "pw_hash" not in u and "salt" not in u
        assert "avatar" not in u, "头像是 data URL，列表里带上纯属浪费带宽"


def test_user_list_filters(admin_client):
    client, token, _ = admin_client
    client.post("/admin/users", json={"username": "zhang3", "password": "pw12345678",
                                      "name": "张三", "role": "ta"}, headers=_auth(token))
    assert all(u["role"] == "ta"
               for u in client.get("/admin/users?role=ta", headers=_auth(token)).json()["items"])
    hit = client.get("/admin/users?q=zhang", headers=_auth(token)).json()
    assert hit["total"] == 1 and hit["items"][0]["name"] == "张三"


@pytest.mark.parametrize("role", ["student", "ta", "teacher"])
def test_non_admin_cannot_manage_users(admin_client, role):
    client, _, _ = admin_client
    token = issue_token("demo_student", role)
    assert client.get("/admin/users", headers=_auth(token)).status_code == 403
    assert client.post("/admin/users", json={"username": "xx", "password": "pw12345678"},
                       headers=_auth(token)).status_code == 403


def test_every_admin_route_requires_admin(admin_client):
    """遍历所有 /admin/* 路由，逐个确认非管理员进不去。

    这条是**防将来**的：新增一个端点时忘了写守卫不会有任何症状，直到有人发现
    非管理员也进得去。写这组用例时就漏过一次，只有逐路由遍历才抓得住 ——
    逐条手写用例必然跟不上新增的端点。

    （守卫现已挂在路由器上，结构上漏不掉；这条用例守的是"以后有人把它拆回
    端点内调用"的情况。）
    """
    client, _, _ = admin_client
    student = issue_token("demo_student", "student")
    checked = 0
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/admin"):
            continue
        url = path.replace("{course_id}", "net").replace("{username}", "demo_student")
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            r = client.request(method, url, json={}, headers=_auth(student))
            assert r.status_code == 403, f"{method} {url} 没有挡住非管理员（得到 {r.status_code}）"
            checked += 1
    assert checked >= 6, f"只检查到 {checked} 个 /admin 端点，路由收集可能失效了"


# --------------------------------------------------------------------------
# 4. CSV 批量导入与班级归一
# --------------------------------------------------------------------------
def _csv(client, token, text: str, apply: bool = False, default_password: str = ""):
    return client.post("/admin/users/import", headers=_auth(token),
                       files={"file": ("名单.csv", text.encode("utf-8"),
                                       "text/csv")},
                       data={"apply": str(apply).lower(),
                             "default_password": default_password})


HEAD = "username,name,student_id,class_name\n"


def test_import_previews_without_writing(admin_client):
    """默认只预览。一次导入 30 个账号，看不到"将建几个"就按下去，
    出错之后没有干净的回退方式 —— 建出来的账号无法批量撤销。"""
    client, token, _ = admin_client
    body = _csv(client, token, HEAD + "s01,张三,2021001,计科2101\ns02,李四,2021002,计科2101\n").json()
    assert body["applied"] is False
    assert body["created"] == 2 and body["skipped"] == 0
    assert all(i["reason"] == "将新建" for i in body["items"])
    assert "预览" in body["note"]
    # 库里不该多出人来
    assert not [u for u in client.get("/admin/users", headers=_auth(token)).json()["items"]
                if u["username"] in ("s01", "s02")]


def test_preview_reports_existing_accounts_instead_of_promising_new_ones(admin_client):
    """预览必须照着 register_user 的判据查一遍重。

    不查的话预览说"将建 12 个"、确认之后实际建出 0 个 —— 而预览的全部意义
    就是让人在写库之前看到会发生什么。实测踩过：拿刚导出的花名册再传一遍，
    预览一路"将新建"。
    """
    client, token, _ = admin_client
    _csv(client, token, HEAD + "s01,张三,2021001,计科2101\n", apply=True)

    body = _csv(client, token, HEAD + "s01,张三,2021001,计科2101\n"          # 用户名撞
                                      "s02,李四,2021001,计科2101\n"          # 学号撞
                                      "s03,王五,2021003,计科2101\n").json()  # 干净
    assert [i["reason"] for i in body["items"]] == [
        "用户名已存在", "该学号已注册过，请勿重复注册", "将新建"]
    assert body["created"] == 1 and body["skipped"] == 2


def test_import_applies_and_creates_students(admin_client):
    client, token, _ = admin_client
    body = _csv(client, token, HEAD + "s01,张三,2021001,计科2101\n", apply=True).json()
    assert body["applied"] is True and body["created"] == 1
    got = [u for u in client.get("/admin/users", headers=_auth(token)).json()["items"]
           if u["username"] == "s01"]
    assert got and got[0]["role"] == "student" and got[0]["class_name"] == "计科2101"
    # 初始密码必须回给教务：系统没有邮件能力，不返回就是建了一批没人知道密码的号
    assert body["items"][0]["password"]
    assert client.post("/auth/login", json={
        "username": "s01", "password": body["items"][0]["password"]}).status_code == 200


def test_one_bad_row_does_not_block_the_rest(admin_client):
    """一行学号重复不该让另外 29 个建不成。"""
    client, token, _ = admin_client
    _csv(client, token, HEAD + "old,老同学,2021001,计科2101\n", apply=True)
    body = _csv(client, token,
                HEAD + "new1,新一,2021002,计科2101\n"
                       "dup,撞学号,2021001,计科2101\n"
                       "new2,新二,2021003,计科2101\n", apply=True).json()
    assert body["created"] == 2 and body["skipped"] == 1
    bad = [i for i in body["items"] if not i["ok"]][0]
    assert bad["username"] == "dup" and "学号" in bad["reason"]


def test_role_column_in_csv_never_grants_privileges(admin_client):
    """一份从邮件里下载来的表格不该成为提权路径 ——
    理由与 auth.py:RegisterReq 拿掉 role 字段完全相同。"""
    client, token, _ = admin_client
    body = _csv(client, token,
                "username,name,role\nsneaky,坏人,admin\n", apply=True).json()
    assert body["created"] == 1
    got = [u for u in client.get("/admin/users", headers=_auth(token)).json()["items"]
           if u["username"] == "sneaky"][0]
    assert got["role"] == "student"
    # 而且要说出来：静默忽略会让人以为它生效了
    assert "role" in body["warning"] and "student" in body["warning"]


def test_duplicate_inside_one_file_is_named_as_such(admin_client):
    """同一份文件里重复：说清楚是文件自身重复，教务才知道去改名单而不是查库。"""
    client, token, _ = admin_client
    body = _csv(client, token, HEAD + "same,甲,1,A班\nsame,乙,2,A班\n").json()
    assert body["items"][1]["reason"] == "本次文件内重复"


def test_import_without_username_column_is_refused(admin_client):
    client, token, _ = admin_client
    r = _csv(client, token, "姓名,班级\n张三,计科2101\n")
    assert r.status_code == 400 and "username" in r.json()["detail"]


def test_batch_class_change_does_not_wipe_other_profile_fields(admin_client):
    """改班只改班。走 update_user_profile 的话，学生的姓名、学院、专业、
    邮箱、手机会被一次性抹掉，而且不会有任何报错。"""
    client, token, _ = admin_client
    _csv(client, token, "username,name,student_id,college,major,class_name\n"
                        "s01,张三,2021001,计算机学院,软件工程,写错的班\n", apply=True)
    r = client.post("/admin/users/class", headers=_auth(token),
                    json={"usernames": ["s01"], "class_name": "计科2101"})
    assert r.status_code == 200 and r.json()["updated"] == 1
    got = [u for u in client.get("/admin/users", headers=_auth(token)).json()["items"]
           if u["username"] == "s01"][0]
    assert got["class_name"] == "计科2101"
    assert got["name"] == "张三" and got["college"] == "计算机学院" and got["major"] == "软件工程"


def test_batch_class_reports_unknown_usernames(admin_client):
    client, token, _ = admin_client
    body = client.post("/admin/users/class", headers=_auth(token),
                       json={"usernames": ["查无此人"], "class_name": "A班"}).json()
    assert body["updated"] == 0 and body["missing"] == ["查无此人"]


def test_class_list_is_public_and_reveals_only_names(admin_client):
    """注册页在登录前就要用它。只回班级名 —— 多回一个字段就是白送一份花名册结构。"""
    client, token, _ = admin_client
    _csv(client, token, HEAD + "s01,张三,2021001,计科2101\n", apply=True)
    body = client.get("/classes").json()          # 不带任何 token
    assert body == {"items": ["计科2101"]}


def test_import_is_admin_only(admin_client):
    client, _, _ = admin_client
    student = issue_token("demo_student", "student")
    assert _csv(client, student, HEAD + "x,y,1,z\n").status_code == 403
    assert client.post("/admin/users/class", headers=_auth(student),
                       json={"usernames": [], "class_name": "A"}).status_code == 403
