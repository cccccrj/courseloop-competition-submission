"""账号维护（建号 / 改角色 / 列表）。

    python -m scripts.manage_users list
    python -m scripts.manage_users create --username jwc --password '***' --name 教务处 --role admin
    python -m scripts.manage_users promote --username zhang --role ta

**为什么第一个管理员必须走命令行**：公网 `/auth/register` 刻意没有 role 字段
（见 backend/api/auth.py:RegisterReq），任何人 POST role="admin" 就能给自己
开管理员的路早就堵死了。之后的账号可以由管理员在 `/admin/users` 建，
但最初那个只能由能登上这台机器的人来建 —— 提权路径不经过任何公网表单。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core import db                             # noqa: E402
from backend.core.security import ROLES                 # noqa: E402


def cmd_list(args) -> int:
    rows = db.list_users(role=args.role or "")
    print(f"{'用户名':<20} {'角色':<10} {'状态':<10} {'姓名':<16} 创建时间")
    for r in rows:
        print(f"{r['username']:<20} {r['role']:<10} {r['status']:<10} "
              f"{(r['name'] or ''):<16} {r['created_at'] or ''}")
    print(f"\n共 {len(rows)} 人")
    return 0


def cmd_create(args) -> int:
    if args.role not in ROLES:
        raise SystemExit(f"✗ 未知角色 {args.role}，可选 {ROLES}")
    if len(args.password) < 8:
        # 比公网注册的 6 位更严：管理员账号能改课表、能建号，口令强度不该更低
        raise SystemExit("✗ 管理员口令至少 8 位")
    ok, msg = db.register_user(args.username, args.password, args.name, role=args.role)
    if not ok:
        raise SystemExit(f"✗ {msg}")
    # 回读确认角色真的落库了。register_user 对不认识的角色是**静默降级**为
    # student 的（那是为公网注册路径留的防线），批量建号这条路上静默降级
    # 意味着建出一个打 /admin/* 全 403 的"管理员"，而现场看起来像权限系统坏了。
    with db.get_conn() as conn:
        got = conn.execute("SELECT role FROM users WHERE username=?",
                           (args.username.strip(),)).fetchone()["role"]
    if got != args.role:
        raise SystemExit(f"✗ 角色未按预期落库：请求 {args.role}，实际 {got}")
    print(f"✓ 已建号 {args.username}（{got}）")
    return 0


def cmd_promote(args) -> int:
    if args.role not in ROLES:
        raise SystemExit(f"✗ 未知角色 {args.role}，可选 {ROLES}")
    if not db.set_user_role(args.username, args.role):
        raise SystemExit(f"✗ 用户 {args.username} 不存在")
    print(f"✓ {args.username} 的角色已改为 {args.role}")
    # 角色写在令牌里，不重登不会生效 —— 这是 security.py 有意的取舍，如实说明
    print("  注意：角色写在令牌里，该用户需要重新登录后新角色才生效。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn in (("list", cmd_list), ("create", cmd_create), ("promote", cmd_promote)):
        p = sub.add_parser(name)
        p.set_defaults(func=fn)
        if name == "list":
            p.add_argument("--role", default="", choices=("",) + ROLES, help="只列某个角色")
        if name == "create":
            p.add_argument("--username", required=True)
            p.add_argument("--password", required=True)
            p.add_argument("--name", default="")
            p.add_argument("--role", default="admin", choices=ROLES)
        if name == "promote":
            p.add_argument("--username", required=True)
            p.add_argument("--role", default="admin", choices=ROLES)

    args = ap.parse_args()
    db.init_db()          # 幂等；保证 status 等后加的列已经迁移到位
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
