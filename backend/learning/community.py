"""学习态势与社区：连续学习天数、周榜、同伴匿名对比。

## 匿名是硬约束，不是选项

榜上除了自己一律不出现用户名、姓名、学号，只有名次。理由不是隐私合规那一套 ——
是这个功能本身：**同伴对比要给的是"我在什么位置"，不是"谁比我强"**。
一旦挂上名字，它就变成了公开排名，而公开排名会让本来就落后的人干脆不打开这一页。
所以后端直接不下发身份字段（见 `_anonymize`），前端想露也露不出来。

## 排名按"做对的题数"，不是作答数

按作答数排，刷一屏错答案就能冲榜；按正确率排，做一道对一道的人永远第一。
做对的题数同时含着"做了多少"和"对了多少"，是这三者里最难刷的一个。
并列名次按标准竞赛规则给（同分同名次，下一名跳号）。

## 没作答的人不上榜

一周没练的学生不会出现在榜上 —— 榜的长度就是本周活跃人数。给他们补一行 0，
既拉长了榜又把"这周没学"写成了"排最后"，那是两回事。自己没练时单独给一句话。

窗口口径直接复用 `weekly_report.week_bounds`：周报说的"本周"和榜上说的"本周"
必须是同一个周一到周日，两处各切各的，学生会拿两个数来问哪个是对的。
"""
from __future__ import annotations

from datetime import date, timedelta

from backend.agents.weekly_report import week_bounds
from backend.core import db
from backend.core.courses import get_course
from backend.learning import study_budget

TREND_DAYS = 14
BOARD_LIMIT = 20


def learning_streak(days: list[str], rest_weekday: int = -1) -> int:
    """连续学习天数：今天没练时允许从昨天开始，避免上午打开中心就被清零。

    原先住在 core_routes 里，只有学习中心一个调用方；现在态势页也要用同一个数，
    挪到这里收口 —— 两处各算各的，就会出现"中心说 5 天、态势页说 4 天"。

    rest_weekday 是用户自己设的休息日（0=周一..6=周日，-1 表示没设）。它被**跳过**：
    既不计入天数，也不中断连续。不这样做的话，产品一边在复习页写着"安心休息"，
    一边因为你真的休息了而把连续天数清零 —— 那等于在惩罚一个我们主动建议的行为。
    注意跳过而非计入：休息日没学习是事实，不该被算成"学了一天"。
    """
    seen = {date.fromisoformat(d) for d in days if d}
    cursor = date.today()
    if cursor not in seen and cursor.weekday() != rest_weekday:
        cursor -= timedelta(days=1)
    streak = 0
    while True:
        if cursor.weekday() == rest_weekday and cursor not in seen:
            # 休息日空着是预期内的，直接跨过去继续往前数。
            cursor -= timedelta(days=1)
            continue
        if cursor not in seen:
            break
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _rank_rows(rows: list[dict]) -> list[dict]:
    """按做对题数排名，并列同名次、下一名跳号（标准竞赛规则）。"""
    ordered = sorted(rows, key=lambda r: (-r["correct_n"], -r["attempts"], r["username"]))
    out = []
    for i, row in enumerate(ordered):
        if i and row["correct_n"] == ordered[i - 1]["correct_n"] \
                and row["attempts"] == ordered[i - 1]["attempts"]:
            rank = out[-1]["rank"]
        else:
            rank = i + 1
        out.append({**row, "rank": rank})
    return out


def _anonymize(row: dict, me: str) -> dict:
    """榜上一行。不是本人时，身份字段一个都不下发。"""
    mine = row["username"] == me
    return {
        "rank": row["rank"], "is_me": mine,
        "alias": "我" if mine else f"第 {row['rank']} 名",
        "attempts": row["attempts"], "correct_n": row["correct_n"],
        "active_days": row["active_days"],
        "correct_rate": round(row["correct_n"] / row["attempts"], 4) if row["attempts"] else 0.0,
        "avg_mastery": row.get("avg_mastery"),
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 4)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 4)


def _trend(username: str, course: str) -> list[dict]:
    """最近 TREND_DAYS 天的每日作答。没记录的日子补 0 —— 折线要连着，
    断点会被读成"这几天数据丢了"，而事实是"这几天没学"。"""
    got = {r["day"]: r for r in db.daily_counts(username, course, TREND_DAYS)}
    today = date.today()
    out = []
    for i in range(TREND_DAYS - 1, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        row = got.get(day)
        out.append({"day": day,
                    "attempts": int(row["attempts"]) if row else 0,
                    "correct_n": int(row["correct_n"]) if row else 0})
    return out


def standings(username: str, course: str, offset: int = 0) -> dict:
    """周榜 + 同伴匿名对比 + 我的学习态势。纯读。"""
    c = get_course(course)
    start, end = week_bounds(offset)

    mastery = db.course_mastery_avg(course)
    rows = [{**r, "avg_mastery": mastery.get(r["username"], {}).get("avg_mastery")}
            for r in db.window_board(course, start, end)]
    ranked = _rank_rows(rows)
    mine = next((r for r in ranked if r["username"] == username), None)

    active_n = len(ranked)
    board = [_anonymize(r, username) for r in ranked[:BOARD_LIMIT]]
    # 自己掉出前 N 名时把自己那一行补在末尾：榜的用处是"我在哪"，
    # 翻不到自己的榜等于没给答案。
    if mine and mine["rank"] > BOARD_LIMIT:
        board.append(_anonymize(mine, username))

    activity = db.learning_activity_summary(username, course)
    streak = learning_streak(activity.get("activity_days") or [],
                             study_budget.load(username)["buffer_weekday"])
    my_mastery = mastery.get(username, {})

    peers_mastery = [v["avg_mastery"] for k, v in mastery.items() if k != username]
    peers_correct = [r["correct_n"] for r in ranked if r["username"] != username]

    return {
        "course": course, "course_display": c.display,
        "week_start": start,
        "week_end": (date.fromisoformat(end) - timedelta(days=1)).isoformat(),
        "offset": offset,
        "active_students": active_n,
        "board": board,
        "me": {
            "on_board": mine is not None,
            "rank": mine["rank"] if mine else None,
            # 百分位 = 我超过了多少比例的**同学**，分母不含自己 —— 含自己的话
            # 三个人里排第一只能算 66.7%，而页面上要说的那句是"你超过了 100% 的同学"。
            # 两种"算不出来"都返回 None，不折成 0：没上榜（这周没练）和
            # 全场只有我一个人活跃（没有可比对象），都不等于"垫底"。
            "percentile": round((active_n - mine["rank"]) / (active_n - 1), 4)
            if mine and active_n > 1 else None,
            "attempts": mine["attempts"] if mine else 0,
            "correct_n": mine["correct_n"] if mine else 0,
            "active_days": mine["active_days"] if mine else 0,
            "correct_rate": round(mine["correct_n"] / mine["attempts"], 4)
            if mine and mine["attempts"] else None,
            "avg_mastery": my_mastery.get("avg_mastery"),
            "points_covered": my_mastery.get("points", 0),
            "streak_days": streak,
        },
        "peers": {
            "median_correct": _median([float(v) for v in peers_correct]),
            "median_mastery": _median([v for v in peers_mastery if v is not None]),
            "learners": len(peers_mastery),
        },
        "trend": _trend(username, course),
    }
