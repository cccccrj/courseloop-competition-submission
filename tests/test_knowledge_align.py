"""知识点对齐与题库灌注的钉子测试。

钉三件事：
1. 别名表和大纲外专题的目标必须是真实存在的知识点 —— 打错一个字就会静默造出孤儿节点；
2. 对齐级联的顺序（别名 → 精确 → 包含 → 相似 → 章节兜底 → 未归类）；
3. `clear_problem_bank(source=...)` 只删本来源 —— 这是 practice_to_bank 能重跑的前提，
   原来那句不带 source 的清库会把真题和 grow_bank 补的题一起清掉。
"""
from __future__ import annotations

import pytest

from backend.core import db
from backend.core.config import settings
from backend.core.courses import DEFAULT_SEED, get_course
from scripts.knowledge_align import (ALIASES, EXTRA_TOPICS, KEYWORD_TOPICS, KnowledgeAligner,
                                     UNCLASSIFIED, stem_key)


def _valid_targets(course_id: str) -> set[str]:
    return set(get_course(course_id).topics) | {t for _, t, _ in EXTRA_TOPICS.get(course_id, [])}


@pytest.mark.parametrize("course_id", [c.id for c in DEFAULT_SEED])
def test_alias_targets_exist(course_id):
    """别名右值必须落在大纲或显式的大纲外专题里，不能凭空引入新名字。"""
    targets = _valid_targets(course_id)
    unknown = {v for v in ALIASES.get(course_id, {}).values() if v not in targets}
    assert not unknown, f"{course_id} 别名指向了不存在的知识点：{unknown}"


@pytest.mark.parametrize("course_id", [c.id for c in DEFAULT_SEED])
def test_extra_topics_do_not_shadow_outline(course_id):
    """大纲外专题不能和大纲里的知识点重名，否则章节归属会互相覆盖。"""
    outline = set(get_course(course_id).topics)
    dupes = {t for _, t, _ in EXTRA_TOPICS.get(course_id, []) if t in outline}
    assert not dupes, f"{course_id} 大纲外专题与大纲重名：{dupes}"


def test_align_cascade_order():
    a = KnowledgeAligner("co")
    assert a.align("大小端存储")[0] == "大小端与数据对齐"      # 别名
    assert a.stats["alias"] == 1
    assert a.align("指令格式")[0] == "指令格式"                 # 精确
    assert a.align("Cache映射")[0] == "Cache映射与替换"         # 包含
    # 认不出的自由文本落到「未归类」，且不会新增大纲外知识点名
    name, chapter, _ = a.align("量子隧穿效应与咖啡因代谢")
    assert (name, chapter) == (UNCLASSIFIED, UNCLASSIFIED)


def test_align_returns_chapter_and_difficulty():
    a = KnowledgeAligner("ds-algo")
    name, chapter, diff = a.align("二分查找")
    assert (name, chapter) == ("顺序与折半查找", "查找")
    assert 1 <= diff <= 5
    # 大纲外专题带自己的章节标签，可审计
    name2, chapter2, _ = a.align("动态规划")
    assert (name2, chapter2) == ("动态规划", "算法专题（大纲外）")


def test_stem_key_ignores_whitespace_and_punctuation():
    assert stem_key("求 x 的值。") == stem_key("求x的值")
    assert stem_key("题目 A") != stem_key("题目 B")


def test_clear_problem_bank_by_source_keeps_other_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "logs_dir", str(tmp_path / "logs"))
    db.init_db()
    keep = db.insert_problem("co", "计算题", "保留：真题一道", 3, "{}", "numeric", source="past-paper")
    drop = db.insert_problem("co", "计算题", "删除：合成一道", 3, "{}", "numeric", source="seed-practice")
    db.add_test_case(drop, "[]", "1", 0)
    removed = db.clear_problem_bank("co", source="seed-practice")
    assert removed == 1
    with db.get_conn() as conn:
        rows = [r[0] for r in conn.execute("SELECT id FROM problems WHERE course_id='co'").fetchall()]
        cases = conn.execute("SELECT COUNT(*) FROM test_cases WHERE problem_id=?", (drop,)).fetchone()[0]
    assert rows == [keep], "只该删 seed-practice，真题必须留着"
    assert cases == 0, "被删题目的 test_cases 要一起清掉"


# ---------- 关键词规则（真题标签是长尾） ----------

@pytest.mark.parametrize("course_id", [c.id for c in DEFAULT_SEED])
def test_keyword_targets_exist(course_id):
    """关键词右值必须是真实知识点。写错一个字它会被静默丢弃，症状是"规则加了但没生效"。"""
    targets = _valid_targets(course_id)
    unknown = {v for _, v in KEYWORD_TOPICS.get(course_id, []) if v not in targets}
    assert not unknown, f"{course_id} 关键词指向了不存在的知识点：{unknown}"


def test_keyword_only_fires_after_precise_matches():
    """关键词是兜底的下一层，不能抢走更准的匹配。"""
    a = KnowledgeAligner("co")
    # "指令格式" 走精确，不该被 ("指令" → 指令格式) 的关键词记账
    assert a.align("指令格式")[0] == "指令格式"
    assert a.stats["exact"] == 1 and a.stats["keyword"] == 0
    # 长尾说法才落到关键词
    assert a.align("微程序控制")[0] == "控制器"
    assert a.align("组相联映像")[0] == "Cache映射与替换"
    assert a.align("DMA传输时间计算")[0] == "程序查询/中断/DMA"
    assert a.stats["keyword"] == 3
    assert a.stats["unclassified"] == 0


def test_keyword_order_is_specific_before_generic():
    """先具体后笼统：'组相联映像' 不能被后面的 '存储' 之类抢走。"""
    a = KnowledgeAligner("co")
    assert a.align("Cache 地址结构")[0] == "Cache映射与替换"
    assert a.align("存储器设计")[0] == "存储器分类"
    assert a.align("DRAM刷新")[0] == "SRAM与DRAM"
