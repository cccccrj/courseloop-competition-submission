"""定向补题：`grow_bank --kind choice` 与它依赖的提示词/缺口口径。

这条链路最容易白跑一整轮而没人发现：题库总数确实涨了，但模考单选段和入学摸底
的缺口一动不动。原因有三个，用例逐个钉住：

1. 代码门（ds-algo）的常规出题 schema 里**根本没有 canonical 字段**，模型只能出
   代码题，落库是 unit_test，组卷不算它是选择题；
2. 缺口如果按 `bank_count`（全部题）数，一个已经有 4 道代码题的知识点会被判成
   "不缺"，而它的选择题其实是 0；
3. "什么算选择题"必须与组卷器同一把尺子，否则脚本报补齐、组卷照旧喊缺。
"""
from __future__ import annotations

import json

import pytest

from backend.agents import practice_generator
from backend.agents.prompts import SHAPE_CHOICE, build_practice_system
from backend.core import db
from backend.core.config import settings
from backend.core.courses import get_course
from backend.learning.mock_exam import is_choice

from scripts.grow_bank import choice_counts

CODE_COURSE = "ds-algo"      # code_enabled
TEXT_COURSE = "co"           # 非代码门


@pytest.fixture()
def bank(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()


# ---------- 提示词定向 ----------

def test_code_course_default_schema_can_express_a_verifiable_non_code_answer():
    """这里原先钉的是反面事实：代码门的默认 schema **没有** final_answer_canonical。

    那是"补了一堆题、模考缺口纹丝不动"的根因，当时用 SHAPE_CHOICE 定向绕开了，默认 schema
    没动。后果在个性化练习里冒了出来：数据结构的概念 / 复杂度类知识点，模型出的非代码题
    既无 canonical 也无 rubric，自检无从核对，/practice/generate 必然报"未通过答案验证"。
    现在默认 schema 两条路都给：代码题走 test_cases，非代码题给 canonical 或 rubric。
    """
    default = build_practice_system(get_course(CODE_COURSE))
    assert "test_cases" in default
    assert "final_answer_canonical" in default and "rubric" in default
    assert "答案必须能被机器核对" in default


def test_choice_shape_replaces_the_code_schema():
    directed = build_practice_system(get_course(CODE_COURSE), SHAPE_CHOICE)
    assert '"final_answer_canonical": {"kind":"choice","value":"B"}' in directed
    # 代码字段要显式清空，否则代码门的模型会照习惯出代码题
    assert '"code": "", "func_name": "", "test_cases": []' in directed
    # answer 只许写字母：_letters 会把解释里的字母一起抓走，写解释必然自检失败
    assert "只写正确选项的字母本身" in directed
    # 选项必须进 stem —— 库里没有 options 列
    assert "写在 stem 里" in directed


def test_default_shape_is_untouched():
    """对照台两侧吃的是同一份生产提示词，默认行为不能被这次改动带偏。"""
    for course in (CODE_COURSE, TEXT_COURSE):
        obj = get_course(course)
        assert build_practice_system(obj) == build_practice_system(obj, "")
    msgs = practice_generator.build_messages("排序", course=CODE_COURSE)
    assert msgs[0]["content"] == build_practice_system(get_course(CODE_COURSE))


def test_shape_reaches_the_model_messages():
    msgs = practice_generator.build_messages("排序", course=CODE_COURSE, shape=SHAPE_CHOICE)
    assert "选择题" in msgs[0]["content"]
    assert '"code": "", "func_name": "", "test_cases": []' in msgs[0]["content"]


# ---------- 入库形态 ----------

def test_choice_payload_lands_as_numeric_even_in_the_code_course(bank):
    """代码门也要能落成 numeric 选择题 —— persist 是按字段而非按课程分派的。"""
    data = {"verified": True, "stem": "下列关于快排的说法正确的是\nA. …\nB. …\nC. …\nD. …",
            "type": "选择题", "difficulty": 2, "answer": "B",
            "final_answer_canonical": {"kind": "choice", "value": "B"},
            "code": "", "func_name": "", "test_cases": []}
    pid = practice_generator.persist(data, CODE_COURSE, "快速排序")
    assert pid

    stored = db.get_problem(pid, with_solution=True)
    assert stored["verification_type"] == "numeric"
    assert is_choice(stored) is True
    # 默认不带 answer：is_choice 拿不到 canonical，会把每道题都判成"偏离题型"
    assert "answer" not in db.get_problem(pid)


# ---------- 缺口口径 ----------

def _linked(course: str, knowledge: str, answer: str, vtype: str) -> int:
    kid = db.upsert_knowledge_point(course, "第一章", knowledge)
    pid = db.insert_problem(course, "题", f"[{knowledge}] {answer[:12]}", 2, answer, vtype,
                            knowledge=knowledge)
    db.link_problem_knowledge(pid, kid)
    return kid


def test_choice_counts_ignores_code_and_non_choice_problems(bank):
    choice = json.dumps({"kind": "choice", "value": "B"})
    kid_a = _linked(CODE_COURSE, "排序", choice, "numeric")
    _linked(CODE_COURSE, "排序", "ref", "unit_test")                       # 代码题不算
    kid_b = _linked(CODE_COURSE, "树", json.dumps({"kind": "int", "value": 3}), "numeric")  # 数值题不算

    counts = choice_counts(CODE_COURSE)
    assert counts == {kid_a: 1}
    assert kid_b not in counts


def test_unlinked_choice_does_not_count(bank):
    """没有 problem_knowledge 关联的题，组卷也用不了；算进来只会把缺口盖住。"""
    db.insert_problem(CODE_COURSE, "选择题", "无归属的选择题", 2,
                      json.dumps({"kind": "choice", "value": "A"}), "numeric",
                      knowledge="排序")
    assert choice_counts(CODE_COURSE) == {}


# ---------- 出处随题入库 ----------
# 出题时本来就检索了教材喂进提示词，但 citations 一直被丢在 grow_bank 里。
# 后果不报错、只是"讲评说不出这题依据哪一页"——2646 块语料和 2950 道题之间
# 没有任何连接。以下用例钉住这条链路的三处：收敛、入库、读出。

_HIT = {"source": "Hello 算法", "title_path": "第 2 章 / 复杂度分析",
        "page_start": 87, "page_end": 88, "doc_type": "textbook",
        "score": 3.14, "excerpt": "时间复杂度描述算法运行时间随数据规模增长的趋势……"}


def _choice_sample(stem: str = "下列关于时间复杂度的说法，正确的是？") -> dict:
    return {"verified": True, "stem": stem, "type": "单项选择题", "difficulty": 2,
            "answer": "B", "final_answer_canonical": {"kind": "choice", "value": "B"}}


def test_citation_payload_keeps_only_locators(bank):
    """只留定位字段。正文在索引里，抄进题库两处会慢慢不一致；score 换个查询就变了。"""
    out = json.loads(practice_generator._citation_payload([_HIT]))
    assert out == [{"source": "Hello 算法", "title_path": "第 2 章 / 复杂度分析",
                    "page_start": 87, "page_end": 88, "doc_type": "textbook"}]
    assert "excerpt" not in out[0] and "score" not in out[0]


def test_no_citation_stores_empty_string_not_an_empty_list(bank):
    """没检索到教材时存空串，不存 "[]" —— 后者在库里看起来像"有出处"。"""
    assert practice_generator._citation_payload(None) == ""
    assert practice_generator._citation_payload([]) == ""
    assert practice_generator._citation_payload([{"title_path": "无 source"}]) == ""


def test_citations_survive_persist_and_come_back_out(bank):
    """三处都要对：写进去、读出来、形状不变。"""
    pid = practice_generator.persist(_choice_sample(), TEXT_COURSE, "时间复杂度分析",
                                     citations=[_HIT])
    assert pid
    got = db.get_problem(pid, with_solution=True)
    assert got["citations"] == [{"source": "Hello 算法", "title_path": "第 2 章 / 复杂度分析",
                                 "page_start": 87, "page_end": 88, "doc_type": "textbook"}]


def test_citations_do_not_break_choice_detection(bank):
    """加了一列不能碰坏组卷判据 —— is_choice 看的仍是 answer 里的 canonical。"""
    pid = practice_generator.persist(_choice_sample(), TEXT_COURSE, "时间复杂度分析",
                                     citations=[_HIT])
    assert is_choice(db.get_problem(pid, with_solution=True))


def test_problems_without_citations_read_back_as_empty(bank):
    """旧题这一列是空串，读取方必须按"没有出处"处理，不能抛异常。"""
    pid = db.insert_problem(TEXT_COURSE, "单项选择题", "旧题：CPU 主频的单位是？", 2,
                            json.dumps({"kind": "choice", "value": "A"}), "numeric")
    assert db.get_problem(pid, with_solution=True)["citations"] == []


def test_code_and_rubric_problems_also_carry_citations(bank):
    """三条入库分支（unit_test / numeric / rule）都要带上，漏一条就是一半题没出处。"""
    code_sample = {"verified": True, "stem": "实现 is_min_heap(arr)", "difficulty": 2,
                   "answer": "遍历非叶子节点", "code": "def is_min_heap(arr):\n    return True",
                   "func_name": "is_min_heap",
                   "test_cases": [{"args": [[1, 2, 3]], "expected": True}]}
    rule_sample = {"verified": True, "stem": "简述进程与线程的区别", "difficulty": 3,
                   "answer": "进程是资源分配单位，线程是调度单位",
                   "rubric": [{"desc": "指出资源分配单位", "score": 5,
                               "evidence": "资源分配"}]}
    for sample, course in ((code_sample, CODE_COURSE), (rule_sample, TEXT_COURSE)):
        pid = practice_generator.persist(sample, course, "知识点", citations=[_HIT])
        assert pid, sample["stem"]
        assert db.get_problem(pid, with_solution=True)["citations"], sample["stem"]


# ---------- 交叉复核 ----------
# 既有门禁验的是"模型的 answer 与它自己的 canonical 一致"——自洽，不是正确。
# 实测 30 道全部通过既有门禁，其中 9 道（30%）标答是错的或题目本身是坏的。
# 以下用例全部打桩，不打 API。

from backend.verification import cross_check


# 用例显式声明阵容，不吃 DEFAULT_JUDGES —— 换默认阵容不该让这些用例变红，
# 它们钉的是"一票否决"这个判据，不是某几个模型。
JUDGES = ("judge-a", "judge-b")


def _stub_judges(monkeypatch, answers: dict[str, str]):
    """把每个复核员钉成固定回答。"""
    monkeypatch.setattr(cross_check, "_ask_one",
                        lambda judge, stem: (judge, answers[judge]))


def test_all_judges_agree_passes(monkeypatch):
    _stub_judges(monkeypatch, {"judge-a": "B", "judge-b": "B"})
    r = cross_check.review("题干", {"kind": "choice", "value": "B"}, JUDGES)
    assert r.passed is True and "2/2" in r.details


def test_one_dissent_vetoes(monkeypatch):
    """一票否决。丢一道对的题只是多跑一次生成，留一道错的题是产品事故 ——
    两侧代价不对称，就不该取中间。"""
    _stub_judges(monkeypatch, {"judge-a": "C", "judge-b": "B"})
    r = cross_check.review("题干", {"kind": "choice", "value": "B"}, JUDGES)
    assert r.passed is False and r.extra["reason"] == "disagree"


def test_broken_question_is_reported_separately(monkeypatch):
    """复核员判「题目本身有问题」与「答案不对」是两种情况，原因要分开记。"""
    _stub_judges(monkeypatch, {"judge-a": "X", "judge-b": "B"})
    r = cross_check.review("自相矛盾的题干", {"kind": "choice", "value": "B"}, JUDGES)
    assert r.passed is False and r.extra["reason"] == "broken"


def test_judge_failure_is_not_agreement(monkeypatch):
    """复核员调用失败不能算它同意 —— 那样一次网络抖动就放行一道没复核过的题。"""
    _stub_judges(monkeypatch, {"judge-a": "?TimeoutError", "judge-b": "B"})
    r = cross_check.review("题干", {"kind": "choice", "value": "B"}, JUDGES)
    assert r.passed is None, "存疑，不是通过"
    assert r.extra["reason"] == "unusable"


def test_multi_answer_choice(monkeypatch):
    _stub_judges(monkeypatch, {"judge-a": "A", "judge-b": "A"})
    assert cross_check.review("多选题", {"kind": "choice", "value": ["A", "C"]}, JUDGES).passed is True
    _stub_judges(monkeypatch, {"judge-a": "B", "judge-b": "A"})
    assert cross_check.review("多选题", {"kind": "choice", "value": ["A", "C"]}, JUDGES).passed is False


def test_default_roster_spans_several_judges():
    """默认阵容不能缩到一个。实测两个同厂系模型会同时漏掉"多个选项都对"那一类，
    加入第三家才抓出来 —— 跨厂比多加同厂模型有用。"""
    assert len(set(cross_check.DEFAULT_JUDGES)) >= 3


def test_empty_canonical_is_unjudgeable(monkeypatch):
    r = cross_check.review("题干", {"kind": "choice", "value": ""}, JUDGES)
    assert r.passed is None and "无从复核" in r.details


def test_grow_bank_drops_questions_that_fail_review(bank, monkeypatch):
    """整条链路：复核不过的题不能落库。"""
    from backend.agents import practice_generator
    from scripts import grow_bank as gb

    sample = {"verified": True, "stem": "复核不过的题", "type": "单项选择题",
              "difficulty": 2, "answer": "B",
              "final_answer_canonical": {"kind": "choice", "value": "B"}}
    monkeypatch.setattr(cross_check, "_ask_one", lambda judge, stem: (judge, "C"))
    verdict = gb.review(sample["stem"], sample["final_answer_canonical"], ("gpt-4o",))
    assert verdict.passed is False
    # 门禁在 persist 之前，所以库里不该出现这道题
    assert not db.problem_id_by_stem(TEXT_COURSE, sample["stem"])
    # 反证：真去 persist 是能进库的，说明拦住它的确实是门禁而不是别的原因
    assert practice_generator.persist(sample, TEXT_COURSE, "知识点")


def test_generation_failure_does_not_kill_the_whole_run(bank, monkeypatch, capsys):
    """一次生成调用失败只作废这一次尝试，不能把整轮带走。

    上一轮全量补题跑到第 826 道时，网关对某段教材片段返
    402「检测到有敏感词」，异常从 `practice_generator.generate` 一路冒到
    main 外面，进程直接没了 —— 那门课剩下 39 个知识点一道都没出，而这一批
    当时已经跑了两个多小时。复核那侧早就是这个规矩（`_ask_one` 失败返 '?'
    不抛），出题这侧漏了。
    """
    import sys as _sys

    from scripts import grow_bank as gb

    # llm_ready 是 property，钉不动，改钉它读的那三个字段。
    monkeypatch.setattr(settings, "llm_base_url", "http://stub")
    monkeypatch.setattr(settings, "llm_api_key", "stub")
    monkeypatch.setattr(settings, "llm_workhorse_model", "stub-model")

    row = {"knowledge_id": 1, "name": "顺序表", "bank_count": 0, "chapter_index": 1}
    monkeypatch.setattr(gb.profile, "build_profile", lambda user, course: [row])
    monkeypatch.setattr(gb, "point_difficulties", lambda course: {1: 2})
    monkeypatch.setattr(gb, "rag_context", lambda q, course: ("教材片段", []))

    def _sensitive_word(*a, **k):
        raise RuntimeError("Error code: 402 - 检测到有敏感词")

    monkeypatch.setattr(gb.practice_generator, "generate", _sensitive_word)
    monkeypatch.setattr(_sys, "argv", ["grow_bank", "--course", TEXT_COURSE,
                                       "--kind", "choice", "--target", "3",
                                       "--max-attempts", "2", "--apply"])

    assert gb.main() == 0                      # 异常没有放出来
    out = capsys.readouterr().out
    assert "生成失败" in out
    # 失败次数与"被门禁否掉"分开报：前者是网关的事，后者是题的质量问题。
    # 混成一个数的话，一次网关抽风会被读成模型质量下降。
    assert "生成调用失败 2" in out


def test_empty_retrieval_never_reaches_the_generator(bank, monkeypatch, capsys):
    """检索为空时不出题，并且在集齐所有角度后就放弃这个知识点。

    上一轮全量补题产出的 1477 道里有 110 道（7.4%）是在 `rag_context` 返回
    0 字符的情况下生成的 —— 模型凭先验编了一道，过了确定性验证也过了交叉复核，
    进库时 `citations` 是空串。os 一门就占 60 道（该门新增的 20%），因为它只有
    104 块语料、没有教材。这条管线的卖点就是"题有教材依据"，所以空上下文必须在
    调模型之前就拦掉：既省钱，也不让无依据的题混进库。
    """
    import sys as _sys

    from scripts import grow_bank as gb

    monkeypatch.setattr(settings, "llm_base_url", "http://stub")
    monkeypatch.setattr(settings, "llm_api_key", "stub")
    monkeypatch.setattr(settings, "llm_workhorse_model", "stub-model")

    row = {"knowledge_id": 1, "name": "分页", "bank_count": 0, "chapter_index": 1}
    monkeypatch.setattr(gb.profile, "build_profile", lambda user, course: [row])
    monkeypatch.setattr(gb, "point_difficulties", lambda course: {1: 2})
    monkeypatch.setattr(gb, "rag_context", lambda q, course: ("   ", []))

    def _must_not_be_called(*a, **k):
        raise AssertionError("检索为空时不该调用出题模型")

    monkeypatch.setattr(gb.practice_generator, "generate", _must_not_be_called)
    # 给的尝试次数远多于角度数，用来验证"集齐角度就 break"确实生效
    monkeypatch.setattr(_sys, "argv", ["grow_bank", "--course", TEXT_COURSE,
                                       "--kind", "choice", "--target", "3",
                                       "--max-attempts", "50", "--apply"])

    assert gb.main() == 0
    out = capsys.readouterr().out
    assert "检索不到教材，跳过" in out
    # 每个角度只试一次就该收手：50 次尝试里只消耗了 len(FACETS) 次
    assert f"检索为空跳过 {len(gb.FACETS)}" in out
    assert "新增 0" in out


def test_a_poisoned_avoid_stem_does_not_block_the_whole_point(bank, monkeypatch, capsys):
    """排除项里有一条会被网关拦下的旧题干时，丢掉排除项也要把题出出来。

    ds-algo「队列」连着两轮 30 次尝试一道题都没补上，日志里全是 402
    「检测到有敏感词」。逐条二分之后定位到：**不是教材片段的问题，是排除项**——
    库里那 6 条旧题干里有一条（一道卡片出队的模拟题）单独送进提示词就必翻，
    另外五条单独送都正常。而排除项每一轮都会原样送进去，于是这个知识点被
    自己库里的一道旧题永久毒死了。

    排除项的作用是压住"同一个知识点连出三道几乎一样的题"，是锦上添花；
    出不出得来题才是底线。所以失败后不带排除项重试一次，并在汇总里单列，
    提示这批题更可能与已有题撞车、值得抽查。
    """
    import sys as _sys

    from scripts import grow_bank as gb

    monkeypatch.setattr(settings, "llm_base_url", "http://stub")
    monkeypatch.setattr(settings, "llm_api_key", "stub")
    monkeypatch.setattr(settings, "llm_workhorse_model", "stub-model")

    row = {"knowledge_id": 1, "name": "队列", "bank_count": 0, "chapter_index": 1}
    monkeypatch.setattr(gb.profile, "build_profile", lambda user, course: [row])
    monkeypatch.setattr(gb, "point_difficulties", lambda course: {1: 2})
    monkeypatch.setattr(gb, "rag_context", lambda q, course: ("教材片段", []))
    monkeypatch.setattr(gb, "existing_stems", lambda course, kid, limit=6: ["会被拦下的旧题干"])
    monkeypatch.setattr(cross_check, "_ask_one", lambda judge, stem: (judge, "B"))

    seen: list[list[str]] = []

    def _gateway(name, **kw):
        seen.append(list(kw.get("avoid") or []))
        if kw.get("avoid"):
            raise RuntimeError("Error code: 402 - 检测到有敏感词")
        return {"verified": True, "stem": f"队列题 {len(seen)}", "type": "单项选择题",
                "difficulty": 2, "answer": "B",
                "final_answer_canonical": {"kind": "choice", "value": "B"}}

    monkeypatch.setattr(gb.practice_generator, "generate", _gateway)
    monkeypatch.setattr(_sys, "argv", ["grow_bank", "--course", TEXT_COURSE,
                                       "--kind", "choice", "--target", "1",
                                       "--max-attempts", "3", "--apply"])

    assert gb.main() == 0
    out = capsys.readouterr().out
    assert "带排除项被网关拦下" in out
    assert "新增 1" in out
    # 退化过的题单列出来，别混进"生成调用失败"里 —— 它其实是出成了的
    assert "去掉排除项才出的 1" in out and "生成调用失败" not in out
    # 先带排除项试、失败才丢掉：顺序反了就等于把去重能力白白丢了
    assert seen == [["会被拦下的旧题干"], []]
