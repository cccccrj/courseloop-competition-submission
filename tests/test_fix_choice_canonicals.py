"""把判分口径已经坏掉的"假选择题"降级出模考单选池。

这批题的危害不是"数据难看"：`mock_exam.is_choice()` 只看 canonical 的 kind，
于是 `{"kind":"choice","value":{"yiaddr":…}}` 这种被标错的计算题、以及标答是
"对"/"②" 的判断题与 ①②③④ 标号真题，都会被抽进**单选段**。而
`numeric_checker._check_choice` 只从作答里抽 A-Z 字母比对：标答是 '对'/'②' 时
学生填任何字母都 `passed=False`（**确定性判错**，成绩单上看不出异常），
标答是 dict 时校验抛异常退化成 LLM 兜底。**主要后果是判错，不是判不了。**

实测在库里有 84 道（co 62 / net 19 / os 3），来自两条路：早于
`ingest_past_papers.canonical_ok()` 那道闸的真题批次，以及根本不走那条路的
`seed-practice`。用例钉住三件事：判据不误伤、只改判分方式不动题、跑两次结果一样。
"""
from __future__ import annotations

import json

import pytest

from backend.core import db
from backend.core.config import settings
from backend.learning.mock_exam import is_choice

from scripts.fix_choice_canonicals import find_broken, main

COURSE = "co"


def _add(value, vtype: str = "numeric", stem: str = "") -> int:
    return db.insert_problem(
        COURSE, "单项选择题", stem or f"题干 {value!r}", 2,
        json.dumps({"kind": "choice", "value": value}, ensure_ascii=False),
        vtype, knowledge="存储器", source="past-paper:测试卷")


@pytest.fixture()
def bank(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()


def _conn():
    import sqlite3
    c = sqlite3.connect(settings.db_path)
    c.row_factory = sqlite3.Row
    return c


def test_finds_the_unjudgeable_and_spares_the_rest(bank):
    """判据必须同时满足"组卷认它是单选"和"判分器认不了它"。"""
    good = _add("B")
    multi = _add(["A", "C"])            # 多选题，canonical 合法，不能动
    dict_value = _add({"src_port": 67})  # 本是计算题，被标成了 choice
    cn_number = _add("②")                # 真题用 ①②③④ 标号，判分器抽不到字母
    word = _add("不能")
    # canonical 不合格、但组卷本来就不要它（rule 题）——与本次症状无关，不该被扫进来
    already_rule = _add({"src_port": 67}, vtype="rule")

    broken = {p["id"] for p in find_broken(_conn())}
    assert broken == {dict_value, cn_number, word}
    assert good not in broken and multi not in broken and already_rule not in broken


def test_apply_only_changes_how_it_is_judged(bank, capsys):
    """降级只动 verification_type：题干、答案、来源都留着，题还在库里。"""
    pid = _add({"src_port": 67}, stem="DHCP ACK 报文的源端口号是多少？")
    before = dict(_conn().execute("SELECT * FROM problems WHERE id=?", (pid,)).fetchone())
    assert is_choice(before)

    assert main_with(["--apply", "--skip-backup"]) == 0

    after = dict(_conn().execute("SELECT * FROM problems WHERE id=?", (pid,)).fetchone())
    assert after["verification_type"] == "rule"
    # 不再被抽进模考单选段 —— 这是这次迁移唯一想要的效果
    assert not is_choice(after)
    for col in ("stem", "answer", "source", "type", "difficulty", "knowledge"):
        assert after[col] == before[col], f"{col} 不该被改动"


def test_second_run_is_a_noop(bank, capsys):
    _add({"src_port": 67})
    assert main_with(["--apply", "--skip-backup"]) == 0
    capsys.readouterr()
    assert main_with([]) == 0
    assert "无（已修复过" in capsys.readouterr().out


def test_course_filter_leaves_other_courses_alone(bank):
    pid = _add({"src_port": 67})
    assert main_with(["--course", "os", "--apply", "--skip-backup"]) == 0
    row = _conn().execute("SELECT verification_type FROM problems WHERE id=?", (pid,)).fetchone()
    assert row["verification_type"] == "numeric"


def main_with(argv: list[str]) -> int:
    """脚本 main() 自己 argparse sys.argv，用例得临时换掉它。"""
    import sys
    saved, sys.argv = sys.argv, ["fix_choice_canonicals", *argv]
    try:
        return main()
    finally:
        sys.argv = saved
