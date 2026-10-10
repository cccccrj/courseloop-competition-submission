"""把 ①②③④ 标号与 对/错 判断题改写成 ABCD，让它们重新可判分。

`fix_choice_canonicals` 只是止血（把判不对的题移出模考单选池），这一步才是治：
库里 50 道题的选项其实是齐的或者只有两个取值，缺的只是 ABCD 这层皮。改完之后
走确定性字母比对，重新进单选池。

用例压在**判据的保守性**上：改坏一道真题比留着它更糟，所以任何一处对不上就跳过，
绝不猜。
"""
from __future__ import annotations

import json

import pytest

from backend.core import db
from backend.core.config import settings
from backend.learning.mock_exam import is_choice

from scripts.normalize_choice_labels import (convert_circled, convert_true_false,
                                             find_fixable, main)

COURSE = "co"


@pytest.fixture()
def bank(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()


def _add(stem: str, value: str, vtype: str = "rule") -> int:
    return db.insert_problem(
        COURSE, "是非判断题", stem, 2,
        json.dumps({"kind": "choice", "value": value}, ensure_ascii=False),
        vtype, knowledge="存储器", source="past-paper:测试卷")


def _conn():
    import sqlite3
    c = sqlite3.connect(settings.db_path)
    c.row_factory = sqlite3.Row
    return c


def main_with(argv: list[str]) -> int:
    import sys
    saved, sys.argv = sys.argv, ["normalize_choice_labels", *argv]
    try:
        return main()
    finally:
        sys.argv = saved


# ---------- 纯函数：判据 ----------

def test_circled_options_become_letters():
    stem = "集中式刷新的缺点是。\n① 控制简单\n② 访存有死区\n③ 按行刷新\n④ 周期漫长"
    got = convert_circled(stem, "②")
    assert got is not None
    new_stem, letter = got
    assert letter == "B"
    assert new_stem.splitlines()[2] == "B. 访存有死区"
    # 四个标号全部换掉，不能只换答案那一个
    assert not any(ch in new_stem for ch in "①②③④")


def test_circled_skips_when_a_label_is_missing_or_repeated():
    """少一个、多一个都跳过 —— 那多半不是四选一，硬改会改出一道错题。"""
    assert convert_circled("只有三项。\n① 甲\n② 乙\n③ 丙", "②") is None
    assert convert_circled("① 甲\n① 甲again\n② 乙\n③ 丙\n④ 丁", "②") is None


def test_circled_skips_labels_that_are_not_at_line_start():
    """正文里顺手写的 ① 不是选项。行首才算。"""
    stem = "下列说法中 ① ② ③ ④ 哪个正确。\n① 甲\n② 乙\n③ 丙\n④ 丁"
    assert convert_circled(stem, "②") is None


def test_true_false_gets_two_real_options():
    stem = "移码主要用于表示浮点数的阶码。 (√表示正确,×表示错误)"
    got = convert_true_false(stem, "对")
    assert got == ("移码主要用于表示浮点数的阶码。\nA. 正确\nB. 错误", "A")
    # 作答说明要去掉：留着它，卷面上就同时有"√/×"和"A/B"两套写法
    assert "√" not in got[0]
    assert convert_true_false("同上。", "错")[1] == "B"


def test_true_false_skips_stems_that_already_have_letter_options():
    """已经有 ABCD 选项行的，说明它本来就是选择题、另有来历，不归本脚本管。"""
    stem = "下列哪项正确。\nA. 甲\nB. 乙\nC. 丙\nD. 丁"
    assert convert_true_false(stem, "对") is None


# ---------- 端到端 ----------

def test_apply_puts_them_back_into_the_choice_pool(bank):
    tf = _add("移码用于阶码。 (√表示正确,×表示错误)", "对")
    circ = _add("缺点是。\n① 甲\n② 乙\n③ 丙\n④ 丁", "③")
    for pid in (tf, circ):
        assert not is_choice(dict(_conn().execute(
            "SELECT * FROM problems WHERE id=?", (pid,)).fetchone()))

    assert main_with(["--apply", "--skip-backup"]) == 0

    for pid, want in ((tf, "A"), (circ, "C")):
        row = dict(_conn().execute("SELECT * FROM problems WHERE id=?", (pid,)).fetchone())
        assert json.loads(row["answer"])["value"] == want
        assert row["verification_type"] == "numeric"
        # 三样必须一起改：只改题干不改 canonical，就从"判不对"变成"必然判错"
        assert is_choice(row)


def test_second_run_is_a_noop(bank, capsys):
    _add("移码用于阶码。", "对")
    assert main_with(["--apply", "--skip-backup"]) == 0
    capsys.readouterr()
    assert main_with([]) == 0
    assert "无（已改过" in capsys.readouterr().out


def test_leaves_unconvertible_answers_alone(bank):
    """结构化答案那一类不在本脚本的能力范围内，必须原样不动。"""
    pid = db.insert_problem(
        COURSE, "计算题", "DHCP ACK 的 yiaddr 是多少？", 2,
        json.dumps({"kind": "choice", "value": {"yiaddr": "10.0.0.1"}}, ensure_ascii=False),
        "rule", knowledge="存储器", source="seed-practice")
    assert not find_fixable(_conn())
    assert main_with(["--apply", "--skip-backup"]) == 0
    row = dict(_conn().execute("SELECT * FROM problems WHERE id=?", (pid,)).fetchone())
    assert row["verification_type"] == "rule"
    assert json.loads(row["answer"])["value"] == {"yiaddr": "10.0.0.1"}
