"""真题草稿机器校对：把 LLM 切出来的题规整成"判分器认得出"的形态。

钉住的是一个会静默降级的坑：选择题如果留成 `verification_type="rule"`，
`mock_exam.is_choice` 不认它（它只认 canonical 的 kind），于是这道真题进了库，
组卷不算它是单选、判分退化成关键词覆盖 —— 题量涨了，能力没涨。
"""
from __future__ import annotations

from scripts.qa_paper_draft import _letters, _truth, normalize, qa


def test_letter_forms_including_answer_with_option_text():
    assert _letters("B") == ["B"]
    assert _letters("(B)") == ["B"]
    assert _letters("b.") == ["B"]
    # 最常见的一种：模型把选项正文也抄了下来
    assert _letters("C. 执行 DMA 操作") == ["C"]
    assert _letters("A、C") == ["A", "C"]
    # 整段论述里出现的字母不算选项
    assert _letters("因为 Cache 命中率高，所以 A 方案更快") == []


def test_truth_forms():
    assert _truth("对") is True and _truth("正确") is True
    assert _truth("错误") is False and _truth("×") is False
    assert _truth("视具体情况而定") is None


def test_choice_becomes_canonical_numeric():
    q, issues = normalize({"type": "单项选择题", "stem": "…( )。 A. x B. y", "answer": "B",
                           "verification_type": "rule", "rubric": [{"desc": "选对", "score": 1}]})
    assert q["verification_type"] == "numeric"
    assert q["canonical"] == {"kind": "choice", "value": "B"}
    assert "rubric" not in q, "选择题不该再留得分点，判分走 canonical"
    assert not issues


def test_true_false_becomes_choice():
    q, _ = normalize({"type": "是非判断题", "stem": "Cache 对程序员透明。", "answer": "对"})
    assert q["canonical"] == {"kind": "choice", "value": "对"}
    assert q["verification_type"] == "numeric"


def test_mislabeled_judgement_with_essay_answer_becomes_essay():
    """模型爱把简答题贴成判断题。答案是整段论述时改判，而不是丢一条噪音告警。"""
    q, issues = normalize({"type": "判断题", "stem": "写出六个中断源的优先权顺序。",
                           "answer": "优先权顺序为 INTR1, INTR2, INTR3, INTR4, INTR5, INTR6。"})
    assert q["type"] == "解答题" and q["verification_type"] == "rule"
    assert any("改判为解答题" in i for i in issues)


def test_placeholder_type_is_backfilled_from_stem():
    q, issues = normalize({"type": "题型", "stem": "下列说法正确的是( )。 A. 甲 B. 乙", "answer": "A"})
    assert q["type"] == "单项选择题"
    assert q["canonical"] == {"kind": "choice", "value": "A"}
    assert any("题型异常" in i for i in issues)

    q2, _ = normalize({"type": "题型", "stem": "简述流水线冲突的三种类型。", "answer": "结构/数据/控制冲突"})
    assert q2["type"] == "解答题"


def test_unanswered_is_reported_and_optionally_dropped():
    items = [{"type": "填空题", "stem": "CRC 校验码是[?]。", "answer": ""},
             {"type": "单项选择题", "stem": "…A. 甲 B. 乙", "answer": "B"}]
    kept, report = qa(items)
    assert report["kept"] == 2 and report["dropped"] == 0
    assert any("缺答案" in "".join(r["issues"]) for r in report["issues"])

    kept2, report2 = qa(items, drop_unanswered=True)
    assert [q["stem"] for q in kept2] == ["…A. 甲 B. 乙"]
    assert report2["dropped"] == 1
    assert report2["choice_count"] == 1


def test_verification_type_is_coerced_to_known_values():
    """LLM 会自造判分类型。`ingest_past_papers` 只认 numeric/unit_test，其余全当 rule ——
    一道写着 "choice" 的选择题会被静默降级成按关键词覆盖判分。"""
    q, issues = normalize({"type": "选择题", "stem": "…A. 甲 B. 乙", "answer": "B",
                           "verification_type": "choice"})
    assert q["verification_type"] == "numeric"
    assert q["canonical"] == {"kind": "choice", "value": "B"}

    q2, issues2 = normalize({"type": "计算题", "stem": "算一算", "answer": "12",
                             "verification_type": None})
    assert q2["verification_type"] == "rule", "没有 canonical 就不能标 numeric，否则判分器判不了"

    q3, issues3 = normalize({"type": "编程题", "stem": "写个函数", "answer": "略",
                             "course": "co", "verification_type": "unit_test"})
    assert q3["verification_type"] == "rule", "计组没有沙箱，unit_test 判不了"
    assert any("unit_test" in i for i in issues3)


# ---------------------------------------------------------------------------
# ingest_past_papers 的入库闸：qa_paper_draft 是第一道，这是最后一道
# ---------------------------------------------------------------------------
# 两道闸挡的是同一类事故，但位置不同。上面那批用例管的是"草稿规整"这一步；
# 下面这批管的是"就算有人跳过规整、直接 load 草稿"也不能出坏数据 ——
# 实测就发生过（本轮我自己跳过了 qa_paper_draft 直接灌库）。

from scripts.ingest_past_papers import _verification_type_of, canonical_ok


def test_canonical_decides_the_verification_type_not_the_declared_field():
    """切题模型会自相矛盾：给了 choice canonical 却把 verification_type 标成 rule。

    照字段入库的后果极隐蔽 —— 题进库了、总量涨了，但 mock_exam.is_choice 要求
    verification_type=="numeric"，于是这批题一道都不进模考单选池，
    脚本报"灌了 37 题"、组卷照旧喊缺。
    """
    assert _verification_type_of(
        {"verification_type": "rule", "canonical": {"kind": "choice", "value": "B"}}) == "numeric"
    assert _verification_type_of({"verification_type": "rule", "answer": "论述题"}) == "rule"
    assert _verification_type_of({}) == "rule"


def test_mistagged_choice_is_downgraded_not_trusted():
    """`{"kind":"choice","value":"0xffeb"}` 是填空题被顺手标成了选择题。

    信了它的后果比少几道题严重：is_choice 只看 kind，会把它抽进模考**单选段**，
    卷面上出现一道没有 ABCD 可选的"单选题"，而 _check_choice 从作答里抽不出字母，
    只能返回"无法判定" —— 一道永远判不了的题。
    """
    for bad in ("0xffeb", "111", "512，1ABC，83.3%", "ROM：2 片，RAM：4 片", "", None):
        canon = {"kind": "choice", "value": bad}
        assert not canonical_ok(canon), bad
        # 降级而不是丢弃：题干和答案都还在，只是不进选择题池
        assert _verification_type_of({"canonical": canon, "answer": str(bad)}) == "rule"


def test_well_formed_choices_still_pass():
    assert canonical_ok({"kind": "choice", "value": "B"})
    assert canonical_ok({"kind": "choice", "value": ["A", "C"]}), "多选题"
    assert canonical_ok({"kind": "int", "value": 7})
    assert not canonical_ok({"kind": "自造类型", "value": 1})
    assert not canonical_ok(None) and not canonical_ok("B")


def test_answerless_questions_never_reach_the_bank(tmp_path, monkeypatch):
    """切题时答案常在解析页，漏抽是常事（实测 235 题里 20 道）。

    这种题入库后判不了分，讲评时"参考："后面是空的 —— 学生只会觉得系统坏了。
    """
    from backend.core import db
    from backend.core.config import settings
    from scripts.ingest_past_papers import insert_questions

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()

    stat = insert_questions([
        {"course": "co", "type": "简答题", "stem": "有答案的题", "answer": "答案在此"},
        {"course": "co", "type": "简答题", "stem": "只有得分点的题", "answer": "",
         "rubric": [{"desc": "要点", "score": 5}]},
        {"course": "co", "type": "简答题", "stem": "既没答案也没得分点的题", "answer": "  "},
    ], source="习题-测试")
    assert stat["skipped"] == 1, stat
    with db.get_conn() as conn:
        stems = {r[0] for r in conn.execute(
            "SELECT stem FROM problems WHERE source='习题-测试'")}
    assert "既没答案也没得分点的题" not in stems
    assert len(stems) == 2
