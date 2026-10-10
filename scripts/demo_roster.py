"""演示花名册：把 12 个 showcase 账号的姓名、学号、班级改成学校真实格式。

## 起因

教务辅导员看过演示之后提的修改要求。原先的演示账号长这样：

    showcase_s01   演示学员01   学号：空   演示一班

**一眼就是假数据。** 辅导员要判断的不是界面好不好看，是"这系统将来装我们班的
数据是什么样"——学号格式对不对、他手上那份名单能不能直接导进去、班级怎么归。
演示数据一眼假，这几件事他一件都判断不了。所以这是**验收数据格式**的要求，
不是审美要求。

## 学号格式

    92 × 106840 × ××
    │  │  │      │  └── 班内序号（2 位）
    │  │  │      └───── 班级（1 位）
    │  │  └──────────── 固定段（学校 / 学院专业代码）
    │  └─────────────── 入学年份末位（2022 级 → 2）
    └────────────────── 固定前缀

    922106840107  =  2022 级 · 1 班 · 07 号

两处是按辅导员那张草图**推断**的，改起来都只是改下面的常量：

- 年份取**一位**（末位）。草图上那个位置只画了一个叉；若实际是两位，
  总长会变成 13 位，改 `YEAR_DIGITS`。
- 末尾三位按 **1 位班级 + 2 位序号** 分。反过来（2 位班级 + 1 位序号）意味着
  一个班最多 9 人，不像；改 `CLASS_DIGITS` / `SEQ_DIGITS`。

## 撞号这件事

编出来的学号理论上可能正好是某个真实学生的。**后果不是数据错乱，是那个真实
学生注册时被挡住**——`db.register_user` 对 student_id 查重，他会看到"该学号已
注册过"。这是一个看得见的失败，不是静默污染，但仍然要处理：
真上线前跑 `bootstrap_demo.py` 的 `_clear_showcase` 把演示账号清掉即可。

## 用法

    python -m scripts.demo_roster                 # 默认 dry-run，只打印花名册
    python -m scripts.demo_roster --apply         # 就地更新已有的 12 个账号
    python -m scripts.demo_roster --csv docs/学生名单模板.csv   # 导出名单模板

导出的 CSV 正是 `/admin/users/import` 吃的那个格式，可以直接拿给辅导员看
"你们的名单按这个存，导进来就行"。仓里那一份在 `docs/学生名单模板.csv`——
放 docs 而不是 data，因为 `data/` 整个在 .gitignore 里，而这份是要交出去的。
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 这个模块**在模块层不 import backend.core.db**：`bootstrap_demo` 要在
# 设好 CL_DB_PATH 之后才导入 db（否则注册会写进默认库），而它需要在模块层
# 就拿到这里的花名册。所以把 db 留到 main() 里再导。

# ---- 学号格式（改这里就够） ----
PREFIX = "92"
MIDDLE = "106840"
YEAR_DIGITS = 1        # 入学年份取末几位
CLASS_DIGITS = 1       # 班级占几位
SEQ_DIGITS = 2         # 班内序号占几位

ENROLL_YEAR = 2022     # 演示这一届
MAJOR = "计算机科学与技术"
COLLEGE = "计算机学院"

# 姓名是编的，不对应任何真人。刻意避开公众人物与过于"网红"的名字：
# 演示时念出来要像一个班的花名册，而不是像一份段子。
ROSTER: list[tuple[str, int, int]] = [
    # (姓名, 班级, 班内序号)
    ("陈昱霖", 1, 3), ("李思远", 1, 7), ("王雨桐", 1, 12),
    ("张浩然", 1, 18), ("刘子涵", 1, 23), ("赵梦琪", 1, 29),
    ("孙嘉懿", 2, 2), ("周静怡", 2, 9), ("吴泽楷", 2, 14),
    ("郑一诺", 2, 20), ("黄书瑶", 2, 26), ("徐文博", 2, 31),
]

USERNAMES = [f"showcase_s{i:02d}" for i in range(1, len(ROSTER) + 1)]


def student_id(year: int, klass: int, seq: int) -> str:
    """按格式拼一个学号。"""
    return (f"{PREFIX}"
            f"{year % (10 ** YEAR_DIGITS):0{YEAR_DIGITS}d}"
            f"{MIDDLE}"
            f"{klass:0{CLASS_DIGITS}d}"
            f"{seq:0{SEQ_DIGITS}d}")


def class_name(klass: int) -> str:
    """班级名。与学号里的班级位一致 —— 两处对不上的话，热力图按班分组会分出
    一个学号说 1 班、名字说 2 班的学生，而这种不一致没有任何报错。"""
    return f"计科{ENROLL_YEAR % 100:02d}{klass:02d}"


def roster() -> list[dict]:
    """完整花名册。username 与既有的 showcase_sNN 对齐，不改账号本身 ——
    改了的话 bootstrap_demo 生成的作答、掌握度、复习计划就全挂在旧名字上了。"""
    return [{"username": USERNAMES[i], "name": name,
             "student_id": student_id(ENROLL_YEAR, klass, seq),
             "class_name": class_name(klass),
             "college": COLLEGE, "major": MAJOR}
            for i, (name, klass, seq) in enumerate(ROSTER)]


def write_csv(path: Path, rows: list[dict]) -> None:
    """导出成 `/admin/users/import` 吃的格式。

    **不写 password 列**：留空让导入页按学号兜底生成，或者填统一初始密码。
    模板里预填一个密码，等于教人把密码写进将来会到处传的表格里。
    """
    cols = ("username", "name", "student_id", "college", "major", "class_name")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in cols})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="就地更新已有账号（默认只打印）")
    ap.add_argument("--csv", default="", help="导出名单模板到这个路径")
    args = ap.parse_args()

    rows = roster()
    print(f"▶ 演示花名册（{ENROLL_YEAR} 级 · 学号格式 "
          f"{PREFIX}{'Y' * YEAR_DIGITS}{MIDDLE}{'C' * CLASS_DIGITS}{'N' * SEQ_DIGITS}）")
    for r in rows:
        print(f"   {r['username']:<14}{r['name']:<8}{r['student_id']:<16}{r['class_name']}")

    if args.csv:
        write_csv(Path(args.csv), rows)
        print(f"\n已导出名单模板：{args.csv}")

    if not args.apply:
        print("\n未写库；确认无误后加 --apply。")
        return 0

    from backend.core import db

    db.init_db()
    missing, updated = [], 0
    with db.get_conn() as conn:
        for r in rows:
            cur = conn.execute(
                "UPDATE users SET name=?, student_id=?, class_name=?, college=?, major=? "
                "WHERE username=?",
                (r["name"], r["student_id"], r["class_name"],
                 r["college"], r["major"], r["username"]))
            if cur.rowcount:
                updated += 1
            else:
                missing.append(r["username"])
        # 已下发的作业按班名精确匹配找人（`db.list_assignments`）。只改学生不改作业的话，
        # 那些作业会指着一个已经没有人的班 —— 学生侧一条都收不到，**而且不会报错**，
        # 现场只会看到"作业卡片不见了"。旧班名硬编在这里而不是猜：它就是这一次改名的来源。
        for old_name, new_name in (("演示一班", class_name(1)), ("演示二班", class_name(2))):
            moved = conn.execute("UPDATE assignments SET class_name=? WHERE class_name=?",
                                 (new_name, old_name)).rowcount
            if moved:
                print(f"   作业改班：{old_name} → {new_name}（{moved} 条）")
    print(f"\n完成：更新 {updated} 个账号"
          + (f"；不存在（先跑 bootstrap_demo）：{'、'.join(missing)}" if missing else "。"))
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
