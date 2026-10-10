from scripts.generate_sft_pilot import (
    COURSES, TARGETS, _choice_matches, _practice_content_reasons, _qa_valid, _record, _slots,
)


def test_pilot_targets_sum_to_500_and_solve_implies_600_calls():
    assert sum(TARGETS.values()) == 500
    assert sum(TARGETS.values()) + TARGETS["solve"] == 600


def test_slots_are_unique_and_balanced_across_courses():
    rows = _slots(150, "practice")
    counts = {course: sum(row["course"] == course for row in rows) for course in COURSES}
    assert len(rows) == len({row["candidate_id"] for row in rows}) == 150
    assert max(counts.values()) - min(counts.values()) <= 1


def test_record_drops_large_context_but_keeps_source_metadata():
    row = _record(
        {"candidate_id": "qa-x", "course": "co", "source_id": "s1", "context": "large"},
        "grounded_qa", [], {"answer": "x"}, True, True, [], {"messages": []},
        drop=("context",),
    )
    assert "context" not in row
    assert row["source_id"] == "s1"
    assert row["accepted"] is True


def test_choice_validation_accepts_letter_or_option_content_not_acronym_noise():
    stem = "结果是（ ） A. -106 B. -105 C. 105 D. CPU 停止"
    canonical = {"kind": "choice", "value": "A"}
    assert _choice_matches("A. -106", stem, canonical)
    assert _choice_matches("-106", stem, canonical)
    assert not _choice_matches("D. CPU 停止", stem, canonical)


def test_qa_accepts_exact_pdf_evidence_with_compact_formula_answer():
    context = "平均时间复杂度为 Theta(n/2) = Theta(n)"
    parsed = {"question": "复杂度？", "answer": "Theta(n)", "abstain": False,
              "citations": [{"source_id": "s1", "evidence": context}]}
    assert _qa_valid(parsed, "s1", context, False)[0] is True


def test_practice_quality_filter_rejects_draft_leakage_and_topic_bypass():
    draft = {"stem": "求结果", "answer": "选项中没有正确答案，修正题干如下", "hint": ""}
    shortcut = {
        "stem": "模拟红黑树，但不需要实现完整结构，只需使用 sorted 返回结果",
        "answer": "升序", "hint": "", "knowledge_points": ["红黑树"],
        "code": "def f(xs): return sorted(xs)",
    }
    clean = {"stem": "计算 2+3", "answer": "5", "hint": "直接相加"}

    assert "draft_or_self_correction_leakage" in _practice_content_reasons(draft, False)
    assert "topic_bypassed_by_trivial_shortcut" in _practice_content_reasons(shortcut, True)
    assert _practice_content_reasons(clean, False) == []
